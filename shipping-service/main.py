from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title = "Shipping Service")

shipments = {}

class TryShippingRequest(BaseModel):
    transaction_id: str
    username: str
    address: str
    fail: bool = False

class TransactionRequest(BaseModel):
    transaction_id: str

@app.get("/health")
def health_check():
    return {
        "service": "shipping-service",
        "status": "UP"
    }

@app.get("/state")
def get_state():
    return {
        "shipments": shipments
    }

@app.post("/tcc/try")
def try_shipping(request: TryShippingRequest):
    if request.fail:
        raise HTTPException(
            status_code = 500,
            detail = "Errore simulato nella spedizione"
        )
    
    if request.transaction_id in shipments:
        return {
            "status": "ALREADY_PREPARED",
            "transaction_id": request.transaction_id
        }
    
    shipments[request.transaction_id] = {
        "username": request.username,
        "address": request.address,
        "status": "TRY"
    }

    return {
        "status": "TRY_OK",
        "transaction_id": request.transaction_id
    }

@app.put("/tcc/confirm")
def confirm_shipping(request: TransactionRequest):
    shipment = shipments.get(request.transaction_id)

    if shipment is None:
        raise HTTPException(
            status_code = 404,
            detail = "Spedizione non trovata"
        )
    
    if shipment["status"] == "CONFIRMED":
        return {
            "status": "ALREADY_CONFIRMED",
            "transaction_id": request.transaction_id
        }
    
    if shipment["status"] == "CANCELLED":
        raise HTTPException(
            status_code = 409,
            detail = "Spedizione già annullata"
        )
    
    shipment["status"] = "CONFIRMED"

    return {
        "status": "CONFIRM_OK",
        "transaction_id": request.transaction_id
    }

@app.post("/tcc/cancel")
def cancel_shipping(request: TransactionRequest):
    shipment = shipments.get(request.transaction_id)

    if shipment is None:
        return {
            "status": "NOTHING_TO_CANCEL",
            "transaction_id": request.transaction_id
        }
    
    if shipment["status"] == "CANCELLED":
        return {
            "status": "ALREADY_CANCELLED",
            "transaction_id": request.transaction_id
        }
    
    if shipment["status"] == "CONFIRMED":
        raise HTTPException(
            status_code = 409,
            detail = "Spedizione già confermata, impossibile annullare"
        )
    
    shipment["status"] = "CANCELLED"

    return {
        "status": "CANCEL_OK",
        "transaction_id": request.transaction_id
    }
