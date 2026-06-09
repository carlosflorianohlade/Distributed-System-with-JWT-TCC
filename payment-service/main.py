from enum import Enum

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="Payment Service")


class PaymentState(str, Enum):
    RESERVED = "RESERVED"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class PaymentTryRequest(BaseModel):
    transaction_id: str = Field(min_length=1)
    username: str = Field(min_length=1)
    amount: float = Field(gt=0)
    fail: bool = False


class TransactionRequest(BaseModel):
    transaction_id: str = Field(min_length=1)


accounts = {
    "gabriele": {
        "balance": 7000.0,
        "blocked": 0.0,
    },
    "carlos": {
        "balance": 10000.0,
        "blocked": 0.0,
    },
}

payments: dict[str, dict] = {}


@app.get("/health")
def health_check():
    return {
        "service": "payment-service",
        "status": "UP",
    }


@app.get("/state")
def get_state():
    return {
        "accounts": accounts,
        "payments": payments,
    }


@app.post("/tcc/try")
def try_payment(request: PaymentTryRequest):
    if request.fail:
        raise HTTPException(
            status_code=500,
            detail="Errore simulato nel pagamento",
        )

    existing = payments.get(request.transaction_id)

    if existing is not None:
        same_payload = (
            existing["username"] == request.username
            and existing["amount"] == request.amount
        )

        if not same_payload:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Transaction ID già usato "
                    "con un payload differente"
                ),
            )

        return {
            "status": "ALREADY_PROCESSED",
            "transaction_id": request.transaction_id,
            "state": existing["state"],
        }

    account = accounts.get(request.username)

    if account is None:
        raise HTTPException(
            status_code=404,
            detail="Account non trovato",
        )

    available = account["balance"] - account["blocked"]

    if available < request.amount:
        raise HTTPException(
            status_code=409,
            detail="Saldo insufficiente",
        )

    account["blocked"] += request.amount

    payments[request.transaction_id] = {
        "transaction_id": request.transaction_id,
        "username": request.username,
        "amount": request.amount,
        "state": PaymentState.RESERVED,
    }

    return {
        "status": "RESERVED",
        "transaction_id": request.transaction_id,
        "state": PaymentState.RESERVED,
    }


@app.put("/tcc/confirm")
def confirm_payment(request: TransactionRequest):
    payment = payments.get(request.transaction_id)

    if payment is None:
        raise HTTPException(
            status_code=404,
            detail="Pagamento non trovato",
        )

    if payment["state"] == PaymentState.CONFIRMED:
        return {
            "status": "ALREADY_CONFIRMED",
            "transaction_id": request.transaction_id,
            "state": PaymentState.CONFIRMED,
        }

    if payment["state"] == PaymentState.CANCELLED:
        raise HTTPException(
            status_code=409,
            detail="Pagamento già annullato",
        )

    account = accounts[payment["username"]]
    amount = payment["amount"]

    account["blocked"] -= amount
    account["balance"] -= amount
    payment["state"] = PaymentState.CONFIRMED

    return {
        "status": "CONFIRMED",
        "transaction_id": request.transaction_id,
        "state": PaymentState.CONFIRMED,
    }


@app.put("/tcc/cancel")
def cancel_payment(request: TransactionRequest):
    payment = payments.get(request.transaction_id)

    if payment is None:
        return {
            "status": "NOTHING_TO_CANCEL",
            "transaction_id": request.transaction_id,
            "state": "IDLE",
            "empty_cancel": True,
        }

    if payment["state"] == PaymentState.CANCELLED:
        return {
            "status": "ALREADY_CANCELLED",
            "transaction_id": request.transaction_id,
            "state": PaymentState.CANCELLED,
        }

    if payment["state"] == PaymentState.CONFIRMED:
        raise HTTPException(
            status_code=409,
            detail="Pagamento già confermato",
        )

    account = accounts[payment["username"]]
    account["blocked"] -= payment["amount"]
    payment["state"] = PaymentState.CANCELLED

    return {
        "status": "CANCELLED",
        "transaction_id": request.transaction_id,
        "state": PaymentState.CANCELLED,
    }
