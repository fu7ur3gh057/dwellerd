from pydantic import BaseModel, Field


# Bound the password length at the schema layer:
#  - min 8 chars rejects empty / single-char attempts before bcrypt fires
#  - max 128 chars caps request size (bcrypt's own limit is 72 bytes;
#    longer plaintext gets silently truncated, so the hash for "a"*200 is
#    the same as "a"*72 — explicit limit makes that surprise impossible).
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int            # access JWT lifetime (seconds)
    username: str
    role: str = "admin"


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class MeResponse(BaseModel):
    username: str
    role: str = "admin"
    user_id: int
    expires_at: int            # access JWT exp (unix timestamp)
    sid: int                   # current server-side session id
