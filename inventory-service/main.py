from copy import deepcopy
from enum import Enum
from threading import Lock
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field


app = FastAPI(title="Inventory Service")


class ReservationState(str, Enum):
    RESERVED = "RESERVED"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class InventoryTryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    quantity: int = Field(gt=0)


class InventoryConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(min_length=1)
    reservation_id: str = Field(min_length=1)


class InventoryCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(min_length=1)
    reservation_id: str | None = None

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
inventory_lock = Lock()


def get_matching_reservation(
    transaction_id: str,
    reservation_id: str,
) -> dict:
    reservation = reservations.get(transaction_id)

    if reservation is None:
        raise HTTPException(
            status_code=404,
            detail="Prenotazione non trovata",
        )

    if reservation["reservation_id"] != reservation_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "reservation_id non corrispondente "
                "alla transazione"
            ),
        )

    return reservation


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
        "available": (
            product["stock"] - product["reserved"]
        ),
    }


@app.get("/state")
def get_state():
    return {
        "products": products,
        "reservations": reservations,
    }


@app.post("/tcc/try")
def try_inventory(request: InventoryTryRequest):
    with inventory_lock:
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
                        "Transaction ID già utilizzato "
                        "con un payload differente"
                    ),
                )

            return {
                "status": "ALREADY_PROCESSED",
                "transaction_id": existing["transaction_id"],
                "reservation_id": existing["reservation_id"],
                "state": existing["state"],
            }

        product = products.get(request.product_id)

        if product is None:
            raise HTTPException(
                status_code=404,
                detail="Prodotto non trovato",
            )

        available = (
            product["stock"] - product["reserved"]
        )

        if available < request.quantity:
            raise HTTPException(
                status_code=409,
                detail="Stock insufficiente",
            )

        reservation_id = str(uuid4())

        product["reserved"] += request.quantity

        reservations[request.transaction_id] = {
            "transaction_id": request.transaction_id,
            "reservation_id": reservation_id,
            "product_id": request.product_id,
            "quantity": request.quantity,
            "state": ReservationState.RESERVED,
        }

        return {
            "status": "RESERVED",
            "transaction_id": request.transaction_id,
            "reservation_id": reservation_id,
            "product_id": request.product_id,
            "quantity": request.quantity,
            "state": ReservationState.RESERVED,
        }


@app.put("/tcc/confirm")
def confirm_inventory(
    request: InventoryConfirmRequest,
):
    with inventory_lock:
        reservation = get_matching_reservation(
            transaction_id=request.transaction_id,
            reservation_id=request.reservation_id,
        )

        if (
            reservation["state"]
            == ReservationState.CONFIRMED
        ):
            return {
                "status": "ALREADY_CONFIRMED",
                "transaction_id": request.transaction_id,
                "reservation_id": request.reservation_id,
                "state": reservation["state"],
            }

        if (
            reservation["state"]
            == ReservationState.CANCELLED
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "La prenotazione è già stata cancellata"
                ),
            )

        product = products[reservation["product_id"]]
        quantity = reservation["quantity"]

        if product["reserved"] < quantity:
            raise HTTPException(
                status_code=500,
                detail="Stato reserved inconsistente",
            )

        if product["stock"] < quantity:
            raise HTTPException(
                status_code=500,
                detail="Stock inconsistente",
            )

        product["reserved"] -= quantity
        product["stock"] -= quantity
        reservation["state"] = ReservationState.CONFIRMED

        return {
            "status": "CONFIRMED",
            "transaction_id": request.transaction_id,
            "reservation_id": request.reservation_id,
            "state": reservation["state"],
        }


@app.put("/tcc/cancel")
def cancel_inventory(
    request: InventoryCancelRequest,
):
    with inventory_lock:
        reservation = reservations.get(
            request.transaction_id
        )

        if reservation is None:
            return {
                "status": "NOTHING_TO_CANCEL",
                "transaction_id": request.transaction_id,
                "state": "IDLE",
                "empty_cancel": True,
            }

        if (
            request.reservation_id is not None
            and reservation["reservation_id"]
            != request.reservation_id
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "reservation_id non corrispondente "
                    "alla transazione"
                ),
            )

        if (
            reservation["state"]
            == ReservationState.CANCELLED
        ):
            return {
                "status": "ALREADY_CANCELLED",
                "transaction_id": request.transaction_id,
                "reservation_id": (
                    reservation["reservation_id"]
                ),
                "state": reservation["state"],
            }

        if (
            reservation["state"]
            == ReservationState.CONFIRMED
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Impossibile cancellare "
                    "una prenotazione già confermata"
                ),
            )

        product = products[reservation["product_id"]]
        quantity = reservation["quantity"]

        if product["reserved"] < quantity:
            raise HTTPException(
                status_code=500,
                detail="Stato reserved inconsistente",
            )

        product["reserved"] -= quantity
        reservation["state"] = ReservationState.CANCELLED

        return {
            "status": "CANCELLED",
            "transaction_id": request.transaction_id,
            "reservation_id": reservation["reservation_id"],
            "state": reservation["state"],
        }