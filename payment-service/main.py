from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title = "Payment Service")

accounts = {
    "gabriele" : {
        "balance" : 7000,
        "blocked" : 0
    },
    "carlos" : {
        "balance" : 10000,
        "blocked" : 0
    }
}

payments = {}

class TryPaymentRequest(BaseModel):
    transaction_id: str
    username: str
    amount: float
    fail: bool = False

class TransactionRequest(BaseModel):
    transaction_id: str

@app.get("/health")
def health_check():
    return {
        "service" : "payment-service",
        "status" : "UP"
    }

@app.get("/state")
def get_state():
    return {
        "accounts" : accounts,
        "payments" : payments
    }

@app.post("/tcc/try")
def try_payment(request: TryPaymentRequest):
    if request.fail:
        raise HTTPException(
            status_code = 500,
            detail = "Errore simulato nel pagamento"
        )
    
    if request.transaction_id in payments:
        return {
            "status" : "ALREADY_BLOCKED",
            "transaction_id" : request.transaction_id
        }
    
    account = accounts.get(request.username)

    if account is None:
        raise HTTPException(
            status_code = 404,
            detail = "Account non trovato"
        )
    
    available = account["balance"] - account["blocked"]

    if available < request.amount:
        raise HTTPException(
            status_code = 409,
            detail = "Saldo insufficiente"
        )
    
    account["blocked"] += request.amount

    payments[request.transaction_id] = {
        "username" : request.username,
        "amount" : request.amount,
        "status" : "TRY"
    }

    return {
        "status" : "TRY_OK",
        "transaction_id" : request.transaction_id
    }

@app.put("/tcc/confirm")
def confirm_payment(request: TransactionRequest):
    payment = payments.get(request.transaction_id)

    if payment is None:
        raise HTTPException(
            status_code = 404,
            detail = "Pagamento non trovato"
        )
    
    if payment["status"] == "CONFIRMED":
        return {
            "status" : "ALREADY_CONFIRMED",
            "transaction_id" : request.transaction_id
        }
    
    if payment["status"] == "CANCELLED":
        raise HTTPException(
            status_code = 409,
            detail = "Pagamento già annullato"
        )
    
    account = accounts[payment["username"]]

    account["blocked"] -= payment["amount"]
    account["balance"] -= payment["amount"]

    payment["status"] = "CONFIRMED"

    return {
        "status" : "CONFIRM_OK",
        "transaction_id" : request.transaction_id
    }

@app.put("/tcc/cancel")
def cancel_payment(request: TransactionRequest):
    payment = payments.get(request.transaction_id)

    if payment is None:
        return {
            "status" : "NOTHING_TO_CANCEL",
            "transaction_id" : request.transaction_id
        }
    
    if payment["status"] == "CANCELLED":
        return {
            "status" : "ALREADY_CANCELLED",
            "transaction_id" : request.transaction_id
        }
    
    if payment["status"] == "CONFIRMED":
        raise HTTPException(
            status_code = 409,
            detail = "Pagamento già confermato, impossibile annullare"
        )
    
    account = accounts[payment["username"]]

    account["blocked"] -= payment["amount"]
    payment["status"] = "CANCELLED"

    return{
        "status" : "CANCEL_OK",
        "transaction_id" : request.transaction_id
    }