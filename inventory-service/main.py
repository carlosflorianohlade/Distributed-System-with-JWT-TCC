from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title = "Inventory Service")

products = {
    "p1": {
        "name": "Laptop",
        "stock": 10,
        "price": 799.99,
        "reserved": 0
    },
    "p2": {
        "name": "Mouse",
        "stock": 20,
        "price": 69.99,
        "reserved" : 0
    },
    "p3": {
        "name": "Keyboard",
        "stock": 7,
        "price": 130.99,
        "reserved" : 0
    }
}

reservations = {}

class TryRequest(BaseModel):
    transaction_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    quantity: int = Field(gt=0)

class TransactionRequest(BaseModel):
    transaction_id: str

@app.get("/health")
def health_check():
    return {
        "service" : "inventory-service",
        "status" : "UP"
    }

@app.get("/state")
def get_state():
    return {
        "products" : products,
        "reservations" : reservations
    }

@app.get("/products/{product_id}")
def get_product(product_id: str):
    product = products.get(product_id)

    if product is None:
        raise HTTPException(
            status_code = 404,
            detail = "Prodotto non trovato"
        )
    
    return {
        "product_id": product_id,
        "name": product["name"],
        "price": product["price"],
        "stock": product["stock"],
        "reserved": product["reserved"],
        "available": product["stock"] - product["reserved"]
    }

@app.post("/tcc/try")
def try_inventory(request: TryRequest):
    if request.transaction_id in reservations:
        return {
            "status" : "ALREADY_RESERVED",
            "transaction_id" : request.transaction_id
        }
    
    product = products.get(request.product_id)

    if product is None:
        raise HTTPException(
            status_code = 404,
            detail = "Prodotto non trovato"
        )
    
    available = product["stock"] - product["reserved"]

    if available < request.quantity:
        raise HTTPException(
            status_code = 409,
            detail = "Stock non disponibile"
        )

    product["reserved"] += request.quantity

    reservations[request.transaction_id] = {
        "product_id" : request.product_id,
        "quantity" : request.quantity,
        "status" : "TRY"
    }

    return {
        "status" : "TRY_OK",
        "transaction_id" : request.transaction_id
    }

@app.put("/tcc/confirm")
def confirm_inventory(request: TransactionRequest):
    reservation = reservations.get(request.transaction_id)

    if reservation is None:
        raise HTTPException(
            status_code = 404,
            detail = "Prenotazione non trovata"
        )
    
    if reservation["status"] == "CONFIRMED":
        return {
            "status" : "ALREADY_CONFIRMED",
            "transaction_id" : request.transaction_id
        }
    
    if reservation["status"] == "CANCELLED":
        raise HTTPException(
            status_code = 409,
            detail = "Prenotazione già annullata"
        )
    
    product = products[reservation["product_id"]]

    product["reserved"] -= reservation["quantity"]
    product["stock"] -= reservation["quantity"]

    reservation["status"] = "CONFIRMED"

    return {
        "status" : "CONFIRM_OK",
        "transaction_id" : request.transaction_id
    }

@app.put("/tcc/cancel")
def cancel_inventory(request: TransactionRequest):
    reservation = reservations.get(request.transaction_id)

    if reservation is None:
        return {
            "status" : "NOTHING_TO_CANCEL",
            "transaction_id" : request.transaction_id
        }
    
    if reservation["status"] == "CANCELLED":
        return {
            "status" : "ALREADY_CANCELLED",
            "transaction_id" : request.transaction_id
        }

    if reservation["status"] == "CONFIRMED":
        raise HTTPException(
            status_code = 409,
            detail = "Prenotazione già confermata, impossibile annullare"
        )
    
    product = products[reservation["product_id"]]

    product["reserved"] -= reservation["quantity"]
    reservation["status"] = "CANCELLED"

    return {
        "status" : "CANCEL_OK",
        "transaction_id" : request.transaction_id
    }