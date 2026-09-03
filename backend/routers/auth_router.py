import logging
from typing import Optional

from pathlib import Path as _Path
from fastapi import APIRouter, Depends, HTTPException, Request, status, UploadFile, File, Form

from middleware.auth_middleware import get_current_user, require_role
from middleware.rate_limiter import rate_limiter
from models.common import SuccessResponse
from models.user import LoginRequest, LoginResponse, UserProfileResponse
from services.auth_service import AuthService
from adapters.db import db, auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])

auth_service = AuthService(db=db, auth_adapter=auth)


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="Register new officer account (public)",
)
async def register(body: dict):
    # Frontend sends: full_name, email, employee_id, department, designation, password, confirm_password
    from datetime import datetime as _dt
    import hashlib as _hashlib
    from utils.helpers import generate_uuid as _gen

    full_name = (body.get("full_name") or body.get("display_name") or "").strip()
    email = (body.get("email") or "").strip()
    employee_id = (body.get("employee_id") or body.get("badge_number") or "").strip()
    department = (body.get("department") or "Karnataka State Police").strip()
    designation = (body.get("designation") or "Officer").strip()
    password = body.get("password") or ""
    confirm = body.get("confirm_password") or body.get("confirmPassword") or ""

    if not full_name or not email or not employee_id or not department or not designation:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="All fields are required")
    if not password or password != confirm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Passwords do not match")
    if len(password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 8 characters")

    try:
        # Use get_all + manual filter to avoid Catalyst query edge cases
        all_users = await db.get_all("Users") or []
        # Debug: log check
        print(f"[REGISTER] Checking email={email} badge={employee_id} against {len(all_users)} existing users")
        for u in all_users:
            em = str(u.get("email", "")).strip().lower()
            if em == email.lower():
                print(f"[REGISTER] Found duplicate email {em} == {email.lower()}")
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists. Please sign in.")
            bm = str(u.get("badge_number", "")).strip().lower()
            if employee_id and bm == employee_id.lower():
                print(f"[REGISTER] Found duplicate badge {bm} == {employee_id.lower()}")
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Employee ID already registered. Please sign in.")

        auth_user_id = None
        try:
            if auth and hasattr(auth, "signup"):
                res = await auth.signup(email=email, password=password, display_name=full_name)
                auth_user_id = res.get("user_id") if isinstance(res, dict) else None
        except Exception as e:
            # signup failed due to duplicate - propagate as conflict
            msg = str(e).lower()
            if "already" in msg or "exists" in msg or "duplicate" in msg or "unique" in msg:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists. Please sign in.")
            auth_user_id = None

        # If auth.signup already created the user (local_auth), it inserted ROWID=user_id
        # In that case we only need to patch badge_number/phone/status and audit log, not re-insert
        if auth_user_id:
            # patch extra fields that local_auth.signup didn't set (badge_number, phone, pending_document status)
            try:
                await db.update("Users", auth_user_id, {
                    "badge_number": employee_id,
                    "phone": body.get("phone") or "",
                    "status": "pending_document",
                    "updated_at": _dt.utcnow().isoformat(),
                })
            except Exception:
                pass  # best-effort, user already exists
            user_id = auth_user_id
            now = _dt.utcnow().isoformat()
        else:
            user_id = _gen()
            now = _dt.utcnow().isoformat()
            pwd_hash = _hashlib.sha256(password.encode()).hexdigest()
            row = {
                "ROWID": user_id,
                "user_id": user_id,
                "display_name": full_name,
                "email": email,
                "badge_number": employee_id,
                "role": "officer",
                "phone": body.get("phone") or "",
                "status": "pending_document",
                "password_hash": pwd_hash,
                "created_at": now,
                "updated_at": now,
            }
            await db.insert("Users", row)
        await db.insert("Audit_Logs", {
            "user_id": user_id,
            "action": "user.registered",
            "module": "auth",
            "details": f"New registration {email} ({employee_id})",
            "created_at": now,
        })
        return {"user_id": user_id, "redirect_url": "/verify-identity", "message": "Registration successful. Please proceed to identity verification."}
    except HTTPException:
        raise
    except Exception as e:
        err_str = str(e)
        if "UNIQUE constraint" in err_str or "already exists" in err_str.lower() or "duplicate" in err_str.lower():
            # Handle race where Catalyst remote already has the email but local query didn't find it
            if "email" in err_str.lower():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists. Please sign in.")
            if "badge" in err_str.lower() or "employee" in err_str.lower():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Employee ID already registered. Please sign in.")
        logger.exception("Registration failed: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Registration failed: {err_str}")


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Authenticate user and return JWT token",
)
async def login(request: Request, body: LoginRequest):
    client_ip = _get_client_ip(request)
    if not rate_limiter.check(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later.",
        )
    try:
        result = await auth_service.login(body.email, body.password)
        return LoginResponse(
            access_token=result["access_token"],
            token_type=result["token_type"],
            expires_in=result["expires_in"],
            user=UserProfileResponse(**result["user"]),
        )
    except ValueError as e:
        msg = str(e)
        low = msg.lower()
        # pending verification should be 403 with structured detail so frontend can redirect to /verify-identity
        if "pending verification" in low or "pending_document" in low or "pending verification" in low:
            # fetch user to get user_id/status for frontend redirect
            try:
                users = await db.query("Users", {"email": body.email})
                u = users[0] if users else None
                uid = (u.get("ROWID") or u.get("user_id")) if u else None
                acct = (u.get("status") or "pending_document") if u else "pending_document"
            except Exception:
                uid = None
                acct = "pending_document"
            detail = {"message": msg, "user_id": uid, "account_status": acct.upper() if isinstance(acct, str) else acct}
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
        if "rejected" in low:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)
        if "suspended" in low or "disabled" in low:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=msg,
        )
    except Exception as e:
        logger.exception("Login failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service unavailable.",
        )


@router.post(
    "/logout",
    response_model=SuccessResponse,
    status_code=status.HTTP_200_OK,
    summary="Logout current user",
)
async def logout(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    try:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "") if auth_header else ""
        await auth_service.logout(token)
        return SuccessResponse(data=None, message="Logged out successfully.")
    except Exception as e:
        logger.exception("Logout failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Logout failed.",
        )


@router.get(
    "/me",
    response_model=UserProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Get current authenticated user profile",
)
async def get_me(current_user: dict = Depends(get_current_user)):
    try:
        user = await auth_service.get_current_user(current_user["user_id"])
        return UserProfileResponse(
            user_id=user.get("ROWID", current_user["user_id"]),
            display_name=user.get("display_name", ""),
            email=user.get("email", ""),
            role=user.get("role", ""),
            badge_number=user.get("badge_number"),
            phone=user.get("phone"),
            status=user.get("status", "active"),
            created_at=user.get("created_at", ""),
            updated_at=user.get("updated_at", ""),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except Exception as e:
        logger.exception("Failed to fetch current user: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve user profile.",
        )


@router.put(
    "/me",
    response_model=UserProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Update current user profile (display name, phone, badge)",
)
async def update_me(body: dict, current_user: dict = Depends(get_current_user)):
    try:
        updated = await auth_service.update_profile(current_user["user_id"], body)
        return UserProfileResponse(
            user_id=updated.get("ROWID", current_user["user_id"]),
            display_name=updated.get("display_name", ""),
            email=updated.get("email", ""),
            role=updated.get("role", ""),
            badge_number=updated.get("badge_number"),
            phone=updated.get("phone"),
            status=updated.get("status", "active"),
            created_at=updated.get("created_at", ""),
            updated_at=updated.get("updated_at", ""),
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception("Failed to update profile: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update profile.")


@router.put(
    "/change-password",
    response_model=SuccessResponse,
    status_code=status.HTTP_200_OK,
    summary="Change password for current user",
)
async def change_password(
    body: dict,
    current_user: dict = Depends(get_current_user),
):
    current_password = body.get("current_password")
    new_password = body.get("new_password")
    confirm_password = body.get("confirm_password")

    if not current_password or not new_password or not confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="current_password, new_password, and confirm_password are required.",
        )
    if new_password != confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="new_password and confirm_password do not match.",
        )
    if len(new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters long.",
        )

    try:
        await auth_service.change_password(
            current_user["user_id"], current_password, new_password
        )
        return SuccessResponse(data=None, message="Password changed successfully.")
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.exception("Change password failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to change password.",
        )


@router.post(
    "/reset-password",
    response_model=SuccessResponse,
    status_code=status.HTTP_200_OK,
    summary="Request password reset email",
)
async def reset_password(body: dict):
    email = body.get("email")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="email is required.",
        )
    try:
        result = await auth_service.reset_password(email)
        return SuccessResponse(
            data={"reset_link": result.get("reset_link"), "reset_token": result.get("reset_token")},
            message=result.get("message", "If the email exists, a password reset link has been sent."),
        )
    except Exception as e:
        logger.exception("Reset password failed: %s", e)
        return SuccessResponse(
            data=None,
            message="If the email exists, a password reset link has been sent.",
        )


@router.post(
    "/reset-password/confirm",
    response_model=SuccessResponse,
    status_code=status.HTTP_200_OK,
    summary="Confirm password reset with token and new password",
)
async def confirm_reset_password(body: dict):
    token = body.get("token")
    new_password = body.get("new_password")
    confirm_password = body.get("confirm_password")
    
    if not token or not new_password or not confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="token, new_password, and confirm_password are required.",
        )
    
    try:
        result = await auth_service.confirm_reset_password(token, new_password, confirm_password)
        return SuccessResponse(data=None, message=result.get("message", "Password reset successful."))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.exception("Confirm reset password failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset password.",
        )


@router.post(
    "/reset-password/direct",
    response_model=SuccessResponse,
    status_code=status.HTTP_200_OK,
    summary="Direct password reset for demo (no token required)",
)
async def direct_reset_password(body: dict):
    email = body.get("email")
    new_password = body.get("new_password")
    confirm_password = body.get("confirm_password")
    
    if not email or not new_password or not confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="email, new_password, and confirm_password are required.",
        )
    
    try:
        result = await auth_service.direct_reset_password(email, new_password, confirm_password)
        return SuccessResponse(data=None, message=result.get("message", "Password reset successful."))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.exception("Direct reset password failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset password.",
        )


# ---- Verification endpoints to satisfy frontend (were previously only in legacy app/auth.py) ----
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/jpg",
}
MAX_FILE_SIZE = 10 * 1024 * 1024


@router.get(
    "/verification-status/{user_id}",
    summary="Get verification status for a user (public, used by IdentityVerificationPage)",
)
async def get_verification_status(user_id: str):
    try:
        user = await db.get("Users", user_id)
        # fallback: try numeric id mapping like usr_006 -> 6
        if not user and user_id.isdigit():
            # search by badge? no, just return 404
            pass
        if not user:
            # also try searching by user_id field via query for hex ids that may be stored as ROWID
            # db.get already checks ROWID, so if not found, 404
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        # Determine storage document existence
        storage_root = _Path(__file__).parent.parent / "storage" / "verification_documents"
        candidates = [
            storage_root / str(user_id),
            storage_root / f"user_{user_id}",
        ]
        if isinstance(user_id, str) and user_id.startswith("usr_"):
            try:
                num = user_id.split("_")[1].lstrip("0") or "0"
                candidates.append(storage_root / f"user_{num}")
            except Exception:
                pass
        doc_attached = False
        doc_file = None
        for cand in candidates:
            if cand.exists() and cand.is_dir():
                try:
                    files = [p for p in cand.iterdir() if p.is_file()]
                    if files:
                        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                        doc_file = files[0]
                        doc_attached = True
                        break
                except Exception:
                    continue
        status_raw = (user.get("status") or "pending_document").lower()
        # Map to frontend AccountStatus
        if status_raw in ("active", "approved", "verified"):
            account_status = "APPROVED"
        elif status_raw in ("pending_document",):
            account_status = "PENDING_DOCUMENT"
        elif status_raw in ("pending_verification", "pending"):
            account_status = "PENDING_VERIFICATION"
        elif status_raw == "rejected":
            account_status = "REJECTED"
        elif status_raw == "suspended":
            account_status = "SUSPENDED"
        else:
            account_status = "PENDING_DOCUMENT" if not doc_attached else "PENDING_VERIFICATION"

        # Document status
        document_status = None
        document = None
        if doc_attached and doc_file is not None:
            if status_raw in ("pending_verification", "pending"):
                document_status = "PENDING"
            elif status_raw in ("active", "approved", "verified"):
                document_status = "APPROVED"
            elif status_raw == "rejected":
                document_status = "REJECTED"
            else:
                document_status = "PENDING"
            # build minimal document response compatible with frontend VerificationDocument
            import mimetypes
            document = {
                "id": 1,
                "user_id": user_id,
                "document_type": "OTHER_GOVERNMENT_ID",
                "original_filename": doc_file.name,
                "stored_filename": doc_file.name,
                "file_size": doc_file.stat().st_size,
                "mime_type": mimetypes.guess_type(str(doc_file))[0] or "application/octet-stream",
                "verification_status": document_status,
                "uploaded_at": user.get("updated_at") or user.get("created_at") or "",
            }
        else:
            # no document yet
            document_status = None

        return {
            "account_status": account_status,
            "document_status": document_status,
            "document": document,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to get verification status for %s: %s", user_id, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve verification status.")


@router.post(
    "/upload-document",
    summary="Upload verification document (public, used by IdentityVerificationPage)",
)
async def upload_verification_document(
    user_id: str = Form(...),
    document_type: str = Form(...),
    file: UploadFile = File(...),
):
    try:
        # Validate user exists
        user = await db.get("Users", user_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        # Validate document_type
        allowed_types = {"EMPLOYEE_ID", "POLICE_ID", "OTHER_GOVERNMENT_ID"}
        if document_type not in allowed_types:
            # accept case-insensitive
            if document_type.upper() not in allowed_types:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid document type")
            document_type = document_type.upper()

        # Allow fallback to extension check (PowerShell Form upload may send generic mime)
        if file.content_type not in ALLOWED_MIME_TYPES:
            ext = _Path(file.filename or "").suffix.lower()
            if ext not in (".pdf", ".jpg", ".jpeg", ".png"):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"File type not allowed. Allowed types: PDF, JPG, JPEG, PNG")
            # extension ok -> allow even if mime is generic like application/octet-stream or text/plain

        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"File size exceeds limit of {MAX_FILE_SIZE // (1024*1024)}MB")

        # Save file
        storage_dir = _Path(__file__).parent.parent / "storage" / "verification_documents" / str(user_id)
        storage_dir.mkdir(parents=True, exist_ok=True)
        # sanitize filename
        orig_name = file.filename or "document"
        # avoid path traversal
        safe_name = _Path(orig_name).name
        dest = storage_dir / safe_name
        # if file exists, make unique
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            dest = storage_dir / f"{stem}_{int(__import__('time').time())}{suffix}"
        dest.write_bytes(content)

        # Update user status to pending_verification
        from datetime import datetime as _dt
        await db.update("Users", user_id, {"status": "pending_verification", "updated_at": _dt.utcnow().isoformat()})
        await db.insert("Audit_Logs", {
            "user_id": user_id,
            "action": "user.document_uploaded",
            "module": "auth",
            "details": f"Verification document uploaded {safe_name} type {document_type}",
            "created_at": _dt.utcnow().isoformat(),
        })

        return {"message": "Document uploaded successfully. Your account is now pending verification.", "document_id": 1, "redirect_url": "/verification-pending"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to upload verification document for %s: %s", user_id, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to upload document.")
