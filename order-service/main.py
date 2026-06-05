from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from jose import jwt, JWTError

app = FastAPI(title = "Order Service")

SECRET_KEY = "super-secret-key"
ALGORITHM = "HS256"

INVENTORY_SERVICE_URL = "http://inventory-service:8000"
PAYMENT_SERVICE_URL = "http://payment-service:8000"
SHIPPING_SERVICE_URL = "http://shipping-service:8000"

orders = {}

class CreateOrderRequest(BaseModel):
    product_id: str
    quantity: int
    address: str
    fail_payment: bool = False
    fail_shipping: bool = False

def verify_token(authorization: str | None) -> dict:
    if authorization is None:
        raise HTTPException(
            status_code = 401,
            detail = "Authorization header mancante"
        )
    
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code = 401,
            detail = "Formato token non valido. Usa: Bearer <token>"
        )
    
    token = authorization.replace("Bearer ", "")

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )
        return payload
    except JWTError:
        raise HTTPException(
            status_code = 401,
            detail = "Token non valido o scaduto"
        )
    
@app.get("/health")
def health_check():
    return {
        "service": "order-service",
        "status": "UP"
    }

@app.get("/orders")
def get_orders():
    return orders

@app.get("/orders/{transaction_id}")
def get_order(transaction_id: str):
    order = orders.get(transaction_id)

    if order is None:
        raise HTTPException(
            status_code = 404,
            detail = "Ordine non trovato"
        )
    
    return order

@app.post("/orders")
async def create_order(
    request: CreateOrderRequest,
    authorization: str | None = Header(default=None, alias="Authorization")
):
    user = verify_token(authorization)

    username = user["sub"]
    transaction_id = str(uuid4())
    completed_steps = []

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            product_response = await client.get(
                f"{INVENTORY_SERVICE_URL}/products/{request.product_id}"
            )
            product_response.raise_for_status()

            product = product_response.json()
            amount = product["price"] * request.quantity

            orders[transaction_id] = {
                "transaction_id": transaction_id,
                "username": username,
                "product_id": request.product_id,
                "quantity": request.quantity,
                "unit_price": product["price"],
                "amount": amount,
                "address": request.address,
                "status": "TRYING",
                "completed_steps": []
            }

            inventory_response = await client.post(
                f"{INVENTORY_SERVICE_URL}/tcc/try",
                json={
                    "transaction_id": transaction_id,
                    "product_id": request.product_id,
                    "quantity": request.quantity
                }
            )
            inventory_response.raise_for_status()
            completed_steps.append("inventory")

            payment_response = await client.post(
                f"{PAYMENT_SERVICE_URL}/tcc/try",
                json={
                    "transaction_id": transaction_id,
                    "username": username,
                    "amount": amount,
                    "fail": request.fail_payment
                }
            )
            payment_response.raise_for_status()
            completed_steps.append("payment")

            shipping_response = await client.post(
                f"{SHIPPING_SERVICE_URL}/tcc/try",
                json={
                    "transaction_id": transaction_id,
                    "username": username,
                    "address": request.address,
                    "fail": request.fail_shipping
                }
            )
            shipping_response.raise_for_status()
            completed_steps.append("shipping")

            orders[transaction_id]["status"] = "CONFIRMING"
            orders[transaction_id]["completed_steps"] = completed_steps

            await confirm_all(client, transaction_id)

            orders[transaction_id]["status"] = "CONFIRMED"

            return {
                "status": "ORDER_CONFIRMED",
                "transaction_id": transaction_id,
                "product_id": request.product_id,
                "quantity": request.quantity,
                "unit_price": product["price"],
                "amount": amount,
                "completed_steps": completed_steps
            }

        except Exception as error:
            if transaction_id not in orders:
                orders[transaction_id] = {
                    "transaction_id": transaction_id,
                    "username": username,
                    "product_id": request.product_id,
                    "quantity": request.quantity,
                    "address": request.address,
                    "status": "CANCELLING",
                    "completed_steps": []
                }

            orders[transaction_id]["status"] = "CANCELLING"
            orders[transaction_id]["error"] = str(error)
            orders[transaction_id]["completed_steps"] = completed_steps

            await cancel_completed_steps(
                client=client,
                transaction_id=transaction_id,
                completed_steps=completed_steps
            )

            orders[transaction_id]["status"] = "CANCELLED"

            raise HTTPException(
                status_code=500,
                detail={
                    "message": "Ordine annullato a causa di un errore durante il TCC",
                    "transaction_id": transaction_id,
                    "completed_steps": completed_steps,
                    "error": str(error)
                }
            )

async def confirm_all(client: httpx.AsyncClient, transaction_id: str):
    body = {"transaction_id": transaction_id}

    await client.put(
        f"{INVENTORY_SERVICE_URL}/tcc/confirm",
        json = body
    ).raise_for_status()

    await client.put(
        f"{PAYMENT_SERVICE_URL}/tcc/confirm",
        json = body
    ).raise_for_status()

    await client.put(
        f"{SHIPPING_SERVICE_URL}/tcc/confirm",
        json = body
    ).raise_for_status()

async def cancel_completed_steps(client: httpx.AsyncClient, transaction_id: str, completed_steps: list[str]):
    body = {"transaction_id": transaction_id}

    if "shipping" in completed_steps:
        await client.put(
            f"{SHIPPING_SERVICE_URL}/tcc/cancel",
            json = body
        )
    
    if "payment" in completed_steps:
        await client.put(
            f"{PAYMENT_SERVICE_URL}/tcc/cancel",
            json = body
        )
    
    if "inventory" in completed_steps:
        await client.put(
            f"{INVENTORY_SERVICE_URL}/tcc/cancel",
            json = body
        )