from enum import Enum
import time
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="Inventory Service")

TTL_SECONDS = int(os.getenv("TTL_SECONDS", "30"))

class ReservationState(str, Enum):
    RESERVED = "RESERVED"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class InventoryTryRequest(BaseModel):
    transaction_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    quantity: int = Field(gt=0)


class TransactionRequest(BaseModel):
    transaction_id: str = Field(min_length=1)


products = {
    "p1": {
        "name": "Laptop",
        "price": 799.99,
        "stock": 10,
        "reserved": 0,
    },
    "p2": {
        "name": "Keyboard",
        "price": 69.99,
        "stock": 30,
        "reserved": 0,
    },
    "p3": {
        "name": "Headphones",
        "price": 130.99,
        "stock": 20,
        "reserved": 0,
    },
}

reservations: dict[str, dict] = {}

def expire_inventory_if_needed(transaction_id: str) -> None:
    reservation = reservations.get(transaction_id)

    if (
        reservation is not None
        and reservation["state"] == ReservationState.RESERVED
        and time.time() >= reservation["expires_at"]
    ):
        product = products[reservation["product_id"]]
        product["reserved"] -= reservation["quantity"]
        reservation["state"] = ReservationState.CANCELLED
        reservation["expired"] = True

@app.get("/health")
def health_check():
    return {
        "service": "inventory-service",
        "status": "UP",
    }


@app.get("/products")
def get_products():
    return products


@app.get("/products/{product_id}")
def get_product(product_id: str):
    product = products.get(product_id)

    if product is None:
        raise HTTPException(
            status_code=404,
            detail="Prodotto non trovato",
        )

    return {
        "product_id": product_id,
        **product,
        "available": product["stock"] - product["reserved"],
    }


@app.get("/state")
def get_state():
    return {
        "products": products,
        "reservations": reservations,
    }


@app.post("/tcc/try")
def try_inventory(request: InventoryTryRequest):
    expire_inventory_if_needed(request.transaction_id)
    existing = reservations.get(request.transaction_id)

    if existing is not None:
        same_payload = (
            existing["product_id"] == request.product_id
            and existing["quantity"] == request.quantity
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

    product = products.get(request.product_id)

    if product is None:
        raise HTTPException(
            status_code=404,
            detail="Prodotto non trovato",
        )

    available = product["stock"] - product["reserved"]

    if available < request.quantity:
        raise HTTPException(
            status_code=409,
            detail="Stock insufficiente",
        )

    product["reserved"] += request.quantity

    reservations[request.transaction_id] = {
        "transaction_id": request.transaction_id,
        "product_id": request.product_id,
        "quantity": request.quantity,
        "state": ReservationState.RESERVED,
        "expires_at": time.time() + TTL_SECONDS,
        "expired": False,
    }

    return {
        "status": "RESERVED",
        "transaction_id": request.transaction_id,
        "state": ReservationState.RESERVED,
    }


@app.put("/tcc/confirm")
def confirm_inventory(request: TransactionRequest):
    expire_inventory_if_needed(request.transaction_id)
    reservation = reservations.get(request.transaction_id)

    if reservation is None:
        raise HTTPException(
            status_code=404,
            detail="Prenotazione non trovata",
        )

    if reservation["state"] == ReservationState.CONFIRMED:
        return {
            "status": "ALREADY_CONFIRMED",
            "transaction_id": request.transaction_id,
            "state": ReservationState.CONFIRMED,
        }

    if reservation["state"] == ReservationState.CANCELLED:
        if reservation.get("expired"):
            raise HTTPException(
                status_code=409,
                detail="Prenotazione scaduta"
            )
        
        raise HTTPException(
            status_code=409,
            detail="Prenotazione già cancellata",
        )

    product = products[reservation["product_id"]]
    quantity = reservation["quantity"]

    product["reserved"] -= quantity
    product["stock"] -= quantity
    reservation["state"] = ReservationState.CONFIRMED

    return {
        "status": "CONFIRMED",
        "transaction_id": request.transaction_id,
        "state": ReservationState.CONFIRMED,
    }


@app.put("/tcc/cancel")
def cancel_inventory(request: TransactionRequest):
    expire_inventory_if_needed(request.transaction_id)
    reservation = reservations.get(request.transaction_id)

    if reservation is None:
        return {
            "status": "NOTHING_TO_CANCEL",
            "transaction_id": request.transaction_id,
            "state": "IDLE",
            "empty_cancel": True,
        }

    if reservation["state"] == ReservationState.CANCELLED:
        return {
            "status": "ALREADY_CANCELLED",
            "transaction_id": request.transaction_id,
            "state": ReservationState.CANCELLED,
        }

    if reservation["state"] == ReservationState.CONFIRMED:
        raise HTTPException(
            status_code=409,
            detail="Prenotazione già confermata",
        )

    product = products[reservation["product_id"]]
    product["reserved"] -= reservation["quantity"]
    reservation["state"] = ReservationState.CANCELLED

    return {
        "status": "CANCELLED",
        "transaction_id": request.transaction_id,
        "state": ReservationState.CANCELLED,
    }
