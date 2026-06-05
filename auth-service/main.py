import os
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from jose import jwt

app = FastAPI(title="Auth Service")

SECRET_KEY = os.environ["JWT_SECRET"]
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

fake_users_db = {
    "gabriele": {
        "username" : "gabriele",
        "password" : "gabrielepass",
        "role" : "customer"
    },
    "carlos": {
        "username" : "carlos",
        "password" : "admin",
        "role" : "admin"
    }
}

class LoginRequest(BaseModel):
    username: str
    password: str

def create_access_token(data: dict) -> str:
    to_encode = data.copy()

    expire = datetime.now(timezone.utc) + timedelta(
        minutes=ACCESS_TOKEN_EXPIRE_MINUTES
    )

    to_encode.update({"exp": expire})

    encoded_jwt = jwt.encode(
        to_encode,
        SECRET_KEY,
        algorithm=ALGORITHM
    )

    return encoded_jwt

@app.get("/health")
def health_check():
    return {
        "service" : "auth-service",
        "status" : "UP"
    }

@app.post("/login")
def login(request: LoginRequest):
    user = fake_users_db.get(request.username)

    if user is None or user["password"] != request.password:
        raise HTTPException(
            status_code=401,
            detail = "Credenziali non valide"
        )
    
    token = create_access_token(
        {
            "sub" : user["username"],
            "role" : user["role"]
        }
    )

    return {
        "access_token" : token,
        "token_type" : "bearer"
    }