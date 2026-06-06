import asyncio
import os
from enum import Enum
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException
from jose import JWTError, jwt
from pydantic import BaseModel, Field


app = FastAPI(title="Order Service")


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

CONFIRM_MAX_ATTEMPTS = int(
    os.getenv("CONFIRM_MAX_ATTEMPTS", "5")
)

CONFIRM_RETRY_DELAY_SECONDS = float(
    os.getenv("CONFIRM_RETRY_DELAY_SECONDS", "1.0")
)

PARTICIPANT_URLS = {
    "inventory": INVENTORY_SERVICE_URL,
    "payment": PAYMENT_SERVICE_URL,
    "shipping": SHIPPING_SERVICE_URL,
}


class TransactionStatus(str, Enum):
    TRYING = "TRYING"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    CANCEL_FAILED = "CANCEL_FAILED"
    CONFIRMING = "CONFIRMING"
    CONFIRMED = "CONFIRMED"
    CONFIRM_PENDING = "CONFIRM_PENDING"


class TransactionDecision(str, Enum):
    UNDECIDED = "UNDECIDED"
    COMMIT = "COMMIT"
    ROLLBACK = "ROLLBACK"


class ParticipantOperationStatus(str, Enum):
    PENDING = "PENDING"
    NOT_STARTED = "NOT_STARTED"
    NOT_REQUIRED = "NOT_REQUIRED"
    OK = "OK"
    FAILED = "FAILED"
    ERROR = "ERROR"
    RETRYING = "RETRYING"


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
        status_code: int = 500,
    ):
        super().__init__(message)
        self.participant = participant
        self.message = message
        self.status_code = status_code


orders: dict[str, dict] = {}


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


def create_transaction(
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
        "status": TransactionStatus.TRYING,
        "decision": TransactionDecision.UNDECIDED,
        "participants": {
            "inventory": {
                "try": ParticipantOperationStatus.PENDING,
                "confirm": ParticipantOperationStatus.NOT_STARTED,
                "cancel": ParticipantOperationStatus.NOT_STARTED,
            },
            "payment": {
                "try": ParticipantOperationStatus.PENDING,
                "confirm": ParticipantOperationStatus.NOT_STARTED,
                "cancel": ParticipantOperationStatus.NOT_STARTED,
            },
            "shipping": {
                "try": ParticipantOperationStatus.PENDING,
                "confirm": ParticipantOperationStatus.NOT_STARTED,
                "cancel": ParticipantOperationStatus.NOT_STARTED,
            },
        },
        "error": None,
        "cancel_errors": {},
        "confirm_errors": {},
    }


def extract_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()

        if isinstance(body, dict):
            detail = body.get("detail")

            if detail is not None:
                return str(detail)

        return str(body)

    except ValueError:
        return response.text or f"HTTP {response.status_code}"


def map_participant_status(status_code: int) -> int:
    if status_code == 404:
        return 404

    if status_code in {409, 422}:
        return 409

    if status_code >= 500:
        return 503

    return 500


async def execute_try_request(
    client: httpx.AsyncClient,
    transaction_id: str,
    participant: str,
    url: str,
    payload: dict,
) -> None:
    transaction = orders[transaction_id]
    participant_state = transaction["participants"][participant]

    try:
        response = await client.post(url, json=payload)

    except httpx.RequestError as error:
        participant_state["try"] = ParticipantOperationStatus.ERROR

        raise TryPhaseError(
            participant=participant,
            message=f"{participant.capitalize()} service non raggiungibile: {error}",
            status_code=503,
        ) from error

    if response.is_error:
        participant_state["try"] = ParticipantOperationStatus.FAILED

        raise TryPhaseError(
            participant=participant,
            message=extract_error_detail(response),
            status_code=map_participant_status(
                response.status_code
            ),
        )

    participant_state["try"] = ParticipantOperationStatus.OK


async def execute_try_phase(
    client: httpx.AsyncClient,
    transaction_id: str,
    username: str,
    request: CreateOrderRequest,
    amount: float,
) -> None:
    await execute_try_request(
        client=client,
        transaction_id=transaction_id,
        participant="inventory",
        url=f"{INVENTORY_SERVICE_URL}/tcc/try",
        payload={
            "transaction_id": transaction_id,
            "product_id": request.product_id,
            "quantity": request.quantity,
        },
    )

    await execute_try_request(
        client=client,
        transaction_id=transaction_id,
        participant="payment",
        url=f"{PAYMENT_SERVICE_URL}/tcc/try",
        payload={
            "transaction_id": transaction_id,
            "username": username,
            "amount": amount,
            "fail": request.fail_payment,
        },
    )

    await execute_try_request(
        client=client,
        transaction_id=transaction_id,
        participant="shipping",
        url=f"{SHIPPING_SERVICE_URL}/tcc/try",
        payload={
            "transaction_id": transaction_id,
            "username": username,
            "address": request.address,
            "fail": request.fail_shipping,
        },
    )


async def cancel_participant(
    client: httpx.AsyncClient,
    participant: str,
    transaction_id: str,
) -> bool:
    transaction = orders[transaction_id]
    participant_state = transaction["participants"][participant]

    try:
        response = await client.put(
            f"{PARTICIPANT_URLS[participant]}/tcc/cancel",
            json={"transaction_id": transaction_id},
        )
        response.raise_for_status()

        participant_state["cancel"] = ParticipantOperationStatus.OK
        transaction["cancel_errors"].pop(participant, None)

        return True

    except httpx.HTTPStatusError as error:
        participant_state["cancel"] = ParticipantOperationStatus.FAILED
        transaction["cancel_errors"][participant] = (
            extract_error_detail(error.response)
        )

        return False

    except httpx.RequestError as error:
        participant_state["cancel"] = ParticipantOperationStatus.ERROR
        transaction["cancel_errors"][participant] = str(error)

        return False


async def execute_cancel_phase(
    client: httpx.AsyncClient,
    transaction_id: str,
) -> bool:
    transaction = orders[transaction_id]

    transaction["decision"] = TransactionDecision.ROLLBACK
    transaction["status"] = TransactionStatus.CANCELLING

    for participant in ("shipping", "payment", "inventory"):
        participant_state = transaction["participants"][participant]

        if participant_state["try"] != ParticipantOperationStatus.OK:
            participant_state["cancel"] = (
                ParticipantOperationStatus.NOT_REQUIRED
            )
            continue

        await cancel_participant(
            client=client,
            participant=participant,
            transaction_id=transaction_id,
        )

    has_failed_cancel = any(
        participant_state["cancel"]
        in {
            ParticipantOperationStatus.FAILED,
            ParticipantOperationStatus.ERROR,
        }
        for participant_state
        in transaction["participants"].values()
    )

    if has_failed_cancel:
        transaction["status"] = TransactionStatus.CANCEL_FAILED
        return False

    transaction["status"] = TransactionStatus.CANCELLED
    return True


async def confirm_participant_with_retry(
    client: httpx.AsyncClient,
    participant: str,
    transaction_id: str,
) -> bool:
    transaction = orders[transaction_id]
    participant_state = transaction["participants"][participant]

    if participant_state["confirm"] == ParticipantOperationStatus.OK:
        return True

    for attempt in range(1, CONFIRM_MAX_ATTEMPTS + 1):
        try:
            response = await client.put(
                f"{PARTICIPANT_URLS[participant]}/tcc/confirm",
                json={"transaction_id": transaction_id},
            )
            response.raise_for_status()

            participant_state["confirm"] = ParticipantOperationStatus.OK
            transaction["confirm_errors"].pop(participant, None)

            return True

        except httpx.HTTPStatusError as error:
            participant_state["confirm"] = (
                ParticipantOperationStatus.RETRYING
            )
            transaction["confirm_errors"][participant] = {
                "attempt": attempt,
                "message": extract_error_detail(
                    error.response
                ),
            }

        except httpx.RequestError as error:
            participant_state["confirm"] = (
                ParticipantOperationStatus.RETRYING
            )
            transaction["confirm_errors"][participant] = {
                "attempt": attempt,
                "message": str(error),
            }

        if attempt < CONFIRM_MAX_ATTEMPTS:
            await asyncio.sleep(
                CONFIRM_RETRY_DELAY_SECONDS
            )

    participant_state["confirm"] = ParticipantOperationStatus.PENDING
    return False


async def execute_confirm_phase(
    client: httpx.AsyncClient,
    transaction_id: str,
) -> bool:
    transaction = orders[transaction_id]

    transaction["decision"] = TransactionDecision.COMMIT
    transaction["status"] = TransactionStatus.CONFIRMING

    for participant in (
        "inventory",
        "payment",
        "shipping",
    ):
        confirmed = await confirm_participant_with_retry(
            client=client,
            participant=participant,
            transaction_id=transaction_id,
        )

        if not confirmed:
            transaction["status"] = (
                TransactionStatus.CONFIRM_PENDING
            )
            return False

    transaction["status"] = TransactionStatus.CONFIRMED
    return True


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
                f"{INVENTORY_SERVICE_URL}/products/"
                f"{request.product_id}"
            )
            product_response.raise_for_status()

        except httpx.HTTPStatusError as error:
            raise HTTPException(
                status_code=map_participant_status(
                    error.response.status_code
                ),
                detail={
                    "message": "Impossibile recuperare il prodotto",
                    "error": extract_error_detail(
                        error.response
                    ),
                },
            ) from error

        except httpx.RequestError as error:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": (
                        "Inventory service non raggiungibile"
                    ),
                    "error": str(error),
                },
            ) from error

        product = product_response.json()
        amount = product["price"] * request.quantity

        orders[transaction_id] = create_transaction(
            transaction_id=transaction_id,
            username=username,
            request=request,
            unit_price=product["price"],
            amount=amount,
        )

        try:
            await execute_try_phase(
                client=client,
                transaction_id=transaction_id,
                username=username,
                request=request,
                amount=amount,
            )

        except TryPhaseError as error:
            transaction = orders[transaction_id]

            transaction["error"] = {
                "phase": "TRY",
                "participant": error.participant,
                "message": error.message,
            }

            rollback_completed = await execute_cancel_phase(
                client=client,
                transaction_id=transaction_id,
            )

            if not rollback_completed:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": (
                            "Il Try è fallito e il rollback "
                            "non è stato completato"
                        ),
                        "transaction_id": transaction_id,
                        "status": transaction["status"],
                        "decision": transaction["decision"],
                        "participants": (
                            transaction["participants"]
                        ),
                        "cancel_errors": (
                            transaction["cancel_errors"]
                        ),
                    },
                ) from error

            raise HTTPException(
                status_code=error.status_code,
                detail={
                    "message": (
                        "Ordine annullato durante la fase Try"
                    ),
                    "transaction_id": transaction_id,
                    "failed_participant": error.participant,
                    "error": error.message,
                    "status": transaction["status"],
                    "decision": transaction["decision"],
                },
            ) from error

        confirmed = await execute_confirm_phase(
            client=client,
            transaction_id=transaction_id,
        )

        transaction = orders[transaction_id]

        if not confirmed:
            return {
                "status": "ORDER_CONFIRM_PENDING",
                "transaction_id": transaction_id,
                "decision": transaction["decision"],
                "participants": transaction["participants"],
                "confirm_errors": transaction["confirm_errors"],
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
