import asyncio
import json
import os
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException
from jose import JWTError, jwt
from pydantic import BaseModel, Field


SECRET_KEY = os.environ["JWT_SECRET"]
ALGORITHM = "HS256"

INVENTORY_SERVICE_URL = os.getenv(
    "INVENTORY_SERVICE_URL",
    "http://inventory-service:8000",
)
PAYMENT_SERVICE_URL = os.getenv(
    "PAYMENT_SERVICE_URL",
    "http://payment-service:8000",
)
SHIPPING_SERVICE_URL = os.getenv(
    "SHIPPING_SERVICE_URL",
    "http://shipping-service:8000",
)

MAX_RETRY_ATTEMPTS = int(os.getenv("MAX_RETRY_ATTEMPTS", "5"))
RETRY_DELAY_SECONDS = float(os.getenv("RETRY_DELAY_SECONDS", "1"))
ORDERS_FILE = Path(os.getenv("ORDERS_FILE", "/data/orders.json"))

PARTICIPANT_URLS = {
    "inventory": INVENTORY_SERVICE_URL,
    "payment": PAYMENT_SERVICE_URL,
    "shipping": SHIPPING_SERVICE_URL,
}


class TransactionStatus(str, Enum):
    TRYING = "TRYING"
    CANCELLING = "CANCELLING"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    CONFIRMING = "CONFIRMING"
    CONFIRM_PENDING = "CONFIRM_PENDING"
    CONFIRMED = "CONFIRMED"


class TransactionDecision(str, Enum):
    UNDECIDED = "UNDECIDED"
    COMMIT = "COMMIT"
    ROLLBACK = "ROLLBACK"


class OperationStatus(str, Enum):
    PENDING = "PENDING"
    OK = "OK"
    FAILED = "FAILED"
    NOT_STARTED = "NOT_STARTED"


class CreateOrderRequest(BaseModel):
    product_id: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    address: str = Field(min_length=1, max_length=300)
    fail_payment: bool = False
    fail_shipping: bool = False


class TryPhaseError(Exception):
    def __init__(
        self,
        participant: str,
        message: str,
        status_code: int,
    ):
        super().__init__(message)
        self.participant = participant
        self.message = message
        self.status_code = status_code


orders: dict[str, dict] = {}


def load_orders() -> None:
    global orders

    if not ORDERS_FILE.exists():
        orders = {}
        return

    try:
        orders = json.loads(ORDERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        orders = {}


def persist_orders() -> None:
    ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = ORDERS_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(orders, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(ORDERS_FILE)


def verify_token(authorization: str | None) -> dict:
    if authorization is None:
        raise HTTPException(
            status_code=401,
            detail="Authorization header mancante",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, separator, token = authorization.partition(" ")

    if (
        not separator
        or scheme.lower() != "bearer"
        or not token.strip()
    ):
        raise HTTPException(
            status_code=401,
            detail="Formato token non valido. Usa: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return jwt.decode(
            token.strip(),
            SECRET_KEY,
            algorithms=[ALGORITHM],
        )
    except JWTError as error:
        raise HTTPException(
            status_code=401,
            detail="Token non valido o scaduto",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


def extract_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("detail") is not None:
            return str(body["detail"])
        return str(body)
    except ValueError:
        return response.text or f"HTTP {response.status_code}"


def map_participant_error(status_code: int) -> int:
    if status_code == 404:
        return 404
    if status_code in {409, 422}:
        return 409
    if status_code >= 500:
        return 503
    return 500


def new_transaction(
    transaction_id: str,
    username: str,
    request: CreateOrderRequest,
    unit_price: float,
    amount: float,
) -> dict:
    return {
        "transaction_id": transaction_id,
        "username": username,
        "product_id": request.product_id,
        "quantity": request.quantity,
        "unit_price": unit_price,
        "amount": amount,
        "address": request.address,
        "status": TransactionStatus.TRYING.value,
        "decision": TransactionDecision.UNDECIDED.value,
        "participants": {
            participant: {
                "try": OperationStatus.PENDING.value,
                "confirm": OperationStatus.NOT_STARTED.value,
                "cancel": OperationStatus.NOT_STARTED.value,
            }
            for participant in PARTICIPANT_URLS
        },
        "last_error": None,
    }


async def try_participant(
    client: httpx.AsyncClient,
    transaction_id: str,
    participant: str,
    payload: dict,
) -> None:
    transaction = orders[transaction_id]

    try:
        response = await client.post(
            f"{PARTICIPANT_URLS[participant]}/tcc/try",
            json=payload,
        )
    except httpx.RequestError as error:
        transaction["participants"][participant]["try"] = (
            OperationStatus.FAILED.value
        )
        persist_orders()
        raise TryPhaseError(
            participant,
            f"{participant} non raggiungibile: {error}",
            503,
        ) from error

    if response.is_error:
        transaction["participants"][participant]["try"] = (
            OperationStatus.FAILED.value
        )
        persist_orders()
        raise TryPhaseError(
            participant,
            extract_error_detail(response),
            map_participant_error(response.status_code),
        )

    transaction["participants"][participant]["try"] = (
        OperationStatus.OK.value
    )
    persist_orders()


async def execute_try_phase(
    client: httpx.AsyncClient,
    transaction_id: str,
    username: str,
    request: CreateOrderRequest,
    amount: float,
) -> None:
    await try_participant(
        client,
        transaction_id,
        "inventory",
        {
            "transaction_id": transaction_id,
            "product_id": request.product_id,
            "quantity": request.quantity,
        },
    )

    await try_participant(
        client,
        transaction_id,
        "payment",
        {
            "transaction_id": transaction_id,
            "username": username,
            "amount": amount,
            "fail": request.fail_payment,
        },
    )

    await try_participant(
        client,
        transaction_id,
        "shipping",
        {
            "transaction_id": transaction_id,
            "username": username,
            "address": request.address,
            "fail": request.fail_shipping,
        },
    )


async def send_with_retry(
    client: httpx.AsyncClient,
    transaction_id: str,
    participant: str,
    operation: str,
) -> bool:
    transaction = orders[transaction_id]

    if (
        transaction["participants"][participant][operation]
        == OperationStatus.OK.value
    ):
        return True

    last_error = None

    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        try:
            response = await client.put(
                f"{PARTICIPANT_URLS[participant]}/tcc/{operation}",
                json={"transaction_id": transaction_id},
            )
            response.raise_for_status()

            transaction["participants"][participant][operation] = (
                OperationStatus.OK.value
            )
            transaction["last_error"] = None
            persist_orders()
            return True

        except httpx.HTTPStatusError as error:
            last_error = extract_error_detail(error.response)
        except httpx.RequestError as error:
            last_error = str(error)

        transaction["participants"][participant][operation] = (
            OperationStatus.FAILED.value
        )
        transaction["last_error"] = {
            "participant": participant,
            "operation": operation,
            "attempt": attempt,
            "message": last_error,
        }
        persist_orders()

        if attempt < MAX_RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_DELAY_SECONDS)

    return False


async def execute_cancel_phase(
    client: httpx.AsyncClient,
    transaction_id: str,
) -> bool:
    transaction = orders[transaction_id]
    transaction["decision"] = TransactionDecision.ROLLBACK.value
    transaction["status"] = TransactionStatus.CANCELLING.value
    persist_orders()

    all_cancelled = True

    # Il Cancel viene inviato a tutti. I participant supportano l'empty-cancel.
    for participant in ("shipping", "payment", "inventory"):
        cancelled = await send_with_retry(
            client,
            transaction_id,
            participant,
            "cancel",
        )
        all_cancelled = all_cancelled and cancelled

    transaction["status"] = (
        TransactionStatus.CANCELLED.value
        if all_cancelled
        else TransactionStatus.CANCEL_PENDING.value
    )
    persist_orders()
    return all_cancelled


async def execute_confirm_phase(
    client: httpx.AsyncClient,
    transaction_id: str,
) -> bool:
    transaction = orders[transaction_id]

    # La decisione COMMIT viene salvata prima di inviare i Confirm.
    transaction["decision"] = TransactionDecision.COMMIT.value
    transaction["status"] = TransactionStatus.CONFIRMING.value
    persist_orders()

    for participant in ("inventory", "payment", "shipping"):
        confirmed = await send_with_retry(
            client,
            transaction_id,
            participant,
            "confirm",
        )

        if not confirmed:
            transaction["status"] = TransactionStatus.CONFIRM_PENDING.value
            persist_orders()
            return False

    transaction["status"] = TransactionStatus.CONFIRMED.value
    persist_orders()
    return True


async def recover_transaction(transaction_id: str) -> bool:
    transaction = orders.get(transaction_id)

    if transaction is None:
        raise HTTPException(
            status_code=404,
            detail="Ordine non trovato",
        )

    async with httpx.AsyncClient(timeout=5.0) as client:
        if transaction["decision"] == TransactionDecision.COMMIT.value:
            return await execute_confirm_phase(client, transaction_id)

        if transaction["decision"] == TransactionDecision.ROLLBACK.value:
            return await execute_cancel_phase(client, transaction_id)

        # Una transazione rimasta TRYING non ha ancora deciso COMMIT:
        # per sicurezza viene portata a ROLLBACK.
        return await execute_cancel_phase(client, transaction_id)


async def recover_pending_transactions() -> None:
    pending_statuses = {
        TransactionStatus.TRYING.value,
        TransactionStatus.CANCELLING.value,
        TransactionStatus.CANCEL_PENDING.value,
        TransactionStatus.CONFIRMING.value,
        TransactionStatus.CONFIRM_PENDING.value,
    }

    for transaction_id, transaction in list(orders.items()):
        if transaction["status"] not in pending_statuses:
            continue

        try:
            await recover_transaction(transaction_id)
        except Exception as error:
            transaction["last_error"] = {
                "operation": "recovery",
                "message": str(error),
            }
            persist_orders()


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_orders()
    await recover_pending_transactions()
    yield


app = FastAPI(
    title="Order Service",
    lifespan=lifespan,
)


@app.get("/health")
def health_check():
    return {
        "service": "order-service",
        "status": "UP",
    }


@app.get("/orders")
def get_orders():
    return orders


@app.get("/orders/{transaction_id}")
def get_order(transaction_id: str):
    order = orders.get(transaction_id)

    if order is None:
        raise HTTPException(
            status_code=404,
            detail="Ordine non trovato",
        )

    return order


@app.post("/orders/{transaction_id}/recover")
async def recover_order(transaction_id: str):
    completed = await recover_transaction(transaction_id)
    transaction = orders[transaction_id]

    return {
        "transaction_id": transaction_id,
        "completed": completed,
        "status": transaction["status"],
        "decision": transaction["decision"],
        "participants": transaction["participants"],
    }


@app.post("/orders")
async def create_order(
    request: CreateOrderRequest,
    authorization: str | None = Header(
        default=None,
        alias="Authorization",
    ),
):
    user = verify_token(authorization)
    username = user.get("sub")

    if not username:
        raise HTTPException(
            status_code=401,
            detail="Il token non contiene il claim sub",
            headers={"WWW-Authenticate": "Bearer"},
        )

    transaction_id = str(uuid4())

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            product_response = await client.get(
                f"{INVENTORY_SERVICE_URL}/products/{request.product_id}"
            )
            product_response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise HTTPException(
                status_code=map_participant_error(
                    error.response.status_code
                ),
                detail=extract_error_detail(error.response),
            ) from error
        except httpx.RequestError as error:
            raise HTTPException(
                status_code=503,
                detail=f"Inventory non raggiungibile: {error}",
            ) from error

        product = product_response.json()
        amount = product["price"] * request.quantity

        orders[transaction_id] = new_transaction(
            transaction_id,
            username,
            request,
            product["price"],
            amount,
        )
        persist_orders()

        try:
            await execute_try_phase(
                client,
                transaction_id,
                username,
                request,
                amount,
            )
        except TryPhaseError as error:
            transaction = orders[transaction_id]
            transaction["last_error"] = {
                "phase": "TRY",
                "participant": error.participant,
                "message": error.message,
            }
            persist_orders()

            rollback_completed = await execute_cancel_phase(
                client,
                transaction_id,
            )

            raise HTTPException(
                status_code=(
                    error.status_code
                    if rollback_completed
                    else 503
                ),
                detail={
                    "message": (
                        "Ordine annullato durante il Try"
                        if rollback_completed
                        else "Rollback non ancora completato"
                    ),
                    "transaction_id": transaction_id,
                    "status": transaction["status"],
                    "decision": transaction["decision"],
                    "failed_participant": error.participant,
                },
            ) from error

        confirmed = await execute_confirm_phase(
            client,
            transaction_id,
        )
        transaction = orders[transaction_id]

        if not confirmed:
            return {
                "status": "ORDER_CONFIRM_PENDING",
                "transaction_id": transaction_id,
                "decision": transaction["decision"],
                "participants": transaction["participants"],
            }

        return {
            "status": "ORDER_CONFIRMED",
            "transaction_id": transaction_id,
            "product_id": request.product_id,
            "quantity": request.quantity,
            "unit_price": product["price"],
            "amount": amount,
            "decision": transaction["decision"],
            "participants": transaction["participants"],
        }
