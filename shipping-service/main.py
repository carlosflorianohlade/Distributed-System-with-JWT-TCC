from enum import Enum

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="Shipping Service")


class ShipmentState(str, Enum):
    RESERVED = "RESERVED"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class ShippingTryRequest(BaseModel):
    transaction_id: str = Field(min_length=1)
    username: str = Field(min_length=1)
    address: str = Field(min_length=1, max_length=300)
    fail: bool = False


class TransactionRequest(BaseModel):
    transaction_id: str = Field(min_length=1)


shipments: dict[str, dict] = {}


@app.get("/health")
def health_check():
    return {
        "service": "shipping-service",
        "status": "UP",
    }


@app.get("/state")
def get_state():
    return {
        "shipments": shipments,
    }


@app.post("/tcc/try")
def try_shipping(request: ShippingTryRequest):
    if request.fail:
        raise HTTPException(
            status_code=500,
            detail="Errore simulato nella spedizione",
        )

    existing = shipments.get(request.transaction_id)

    if existing is not None:
        same_payload = (
            existing["username"] == request.username
            and existing["address"] == request.address
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

    shipments[request.transaction_id] = {
        "transaction_id": request.transaction_id,
        "username": request.username,
        "address": request.address,
        "state": ShipmentState.RESERVED,
    }

    return {
        "status": "RESERVED",
        "transaction_id": request.transaction_id,
        "state": ShipmentState.RESERVED,
    }


@app.put("/tcc/confirm")
def confirm_shipping(request: TransactionRequest):
    shipment = shipments.get(request.transaction_id)

    if shipment is None:
        raise HTTPException(
            status_code=404,
            detail="Spedizione non trovata",
        )

    if shipment["state"] == ShipmentState.CONFIRMED:
        return {
            "status": "ALREADY_CONFIRMED",
            "transaction_id": request.transaction_id,
            "state": ShipmentState.CONFIRMED,
        }

    if shipment["state"] == ShipmentState.CANCELLED:
        raise HTTPException(
            status_code=409,
            detail="Spedizione già annullata",
        )

    shipment["state"] = ShipmentState.CONFIRMED

    return {
        "status": "CONFIRMED",
        "transaction_id": request.transaction_id,
        "state": ShipmentState.CONFIRMED,
    }


@app.put("/tcc/cancel")
def cancel_shipping(request: TransactionRequest):
    shipment = shipments.get(request.transaction_id)

    if shipment is None:
        return {
            "status": "NOTHING_TO_CANCEL",
            "transaction_id": request.transaction_id,
            "state": "IDLE",
            "empty_cancel": True,
        }

    if shipment["state"] == ShipmentState.CANCELLED:
        return {
            "status": "ALREADY_CANCELLED",
            "transaction_id": request.transaction_id,
            "state": ShipmentState.CANCELLED,
        }

    if shipment["state"] == ShipmentState.CONFIRMED:
        raise HTTPException(
            status_code=409,
            detail="Spedizione già confermata",
        )

    shipment["state"] = ShipmentState.CANCELLED

    return {
        "status": "CANCELLED",
        "transaction_id": request.transaction_id,
        "state": ShipmentState.CANCELLED,
    }
