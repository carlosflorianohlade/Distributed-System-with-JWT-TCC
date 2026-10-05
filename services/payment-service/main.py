from enum import Enum
import logging
import os
import time

import stripe
from fastapi import FastAPI, HTTPException, Path
from pydantic import BaseModel, Field


logger = logging.getLogger("payment-service")

app = FastAPI(title="Payment Service")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


TTL_SECONDS = int(os.getenv("TTL_SECONDS", "30"))

# --- Configurazione Stripe (sandbox) ---------------------------------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_CURRENCY = os.getenv("STRIPE_CURRENCY", "eur").lower()
STRIPE_DEFAULT_PAYMENT_METHOD = os.getenv(
    "STRIPE_DEFAULT_PAYMENT_METHOD", "pm_card_visa"
)

if not STRIPE_SECRET_KEY.startswith(("sk_test_", "rk_test_")):
    # Diagnosi senza mai stampare la chiave
    if not STRIPE_SECRET_KEY:
        reason = (
            "la variabile e' vuota o non arriva al container "
            "(controlla 'environment' del payment-service nel docker-compose.yml)"
        )
    elif STRIPE_SECRET_KEY.startswith(("sk_live_", "rk_live_")):
        reason = "e' una chiave LIVE: sono ammesse solo chiavi sandbox"
    elif STRIPE_SECRET_KEY.startswith("pk_"):
        reason = "e' la chiave pubblicabile (pk_): serve quella segreta (sk_test_)"
    else:
        reason = "formato non riconosciuto: deve iniziare con sk_test_"
    raise RuntimeError(f"STRIPE_SECRET_KEY non valida: {reason}.")

stripe.api_key = STRIPE_SECRET_KEY
stripe.max_network_retries = 2


class PaymentState(str, Enum):
    RESERVED = "RESERVED"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class PaymentTryRequest(BaseModel):
    transaction_id: str = Field(min_length=1)
    username: str = Field(min_length=1)
    amount: float = Field(gt=0)
    fail: bool = False
    payment_method: str | None = None


class TransactionRequest(BaseModel):
    transaction_id: str = Field(min_length=1)


class UserCardRequest(BaseModel):
    # Carta di test associata all'utente, ad esempio:
    #   pm_card_visa                                -> pagamenti sempre accettati
    #   pm_card_chargeDeclinedInsufficientFunds     -> rifiutata: fondi insufficienti
    payment_method: str = Field(
        default=STRIPE_DEFAULT_PAYMENT_METHOD,
        pattern=r"^pm_card_[A-Za-z0-9_]+$",
    )


payments: dict[str, dict] = {}

# Utenti registrati su Stripe: username -> {customer_id, payment_method}.
# Cache in memoria; la fonte di verità è il Customer su Stripe (cercato per
# email), quindi la scelta della carta sopravvive al riavvio del container.
users: dict[str, dict | None] = {}
USER_EMAIL_DOMAIN = "tcc-demo.test"


# --- Helper Stripe ---------------------------------------------------------

def to_minor_units(amount: float) -> int:
    """Euro -> centesimi (Stripe lavora in unità minori)."""
    return int(round(amount * 100))


def stripe_http_error(error: stripe.StripeError) -> HTTPException:
    """Traduce gli errori Stripe in codici che l'order-service sa gestire."""
    if isinstance(error, stripe.CardError):
        return HTTPException(
            status_code=409,
            detail=f"Pagamento rifiutato: {error.user_message or error.code}",
        )
    if isinstance(error, stripe.InvalidRequestError):
        return HTTPException(
            status_code=409,
            detail=f"Richiesta Stripe non valida: {error.user_message or error}",
        )
    if isinstance(error, (stripe.APIConnectionError, stripe.RateLimitError)):
        return HTTPException(
            status_code=503, detail="Stripe non raggiungibile, riprovare"
        )
    return HTTPException(status_code=502, detail=f"Errore Stripe: {error}")


def user_email(username: str) -> str:
    return f"{username}@{USER_EMAIL_DOMAIN}"


def customer_to_user(username: str, customer) -> dict:
    metadata = getattr(customer, "metadata", None) or {}
    return {
        "username": username,
        "customer_id": customer.id,
        "payment_method": metadata.to_dict().get(
            "test_payment_method", STRIPE_DEFAULT_PAYMENT_METHOD
        ),
    }


def load_user(username: str) -> dict | None:
    """Restituisce l'utente registrato (cache, poi Stripe) oppure None."""
    if username in users:
        return users[username]

    try:
        found = stripe.Customer.list(email=user_email(username), limit=1)
    except stripe.StripeError as error:
        raise stripe_http_error(error) from error

    user = customer_to_user(username, found.data[0]) if found.data else None
    users[username] = user
    return user


def stripe_cancel_quietly(payment_intent_id: str) -> None:
    try:
        stripe.PaymentIntent.cancel(payment_intent_id)
    except stripe.StripeError as error:
        logger.warning("Cancel PaymentIntent %s: %s", payment_intent_id, error)


def expire_payment_if_needed(transaction_id: str) -> None:
    payment = payments.get(transaction_id)

    if (
        payment is not None
        and payment["state"] == PaymentState.RESERVED
        and time.time() >= payment["expires_at"]
    ):
        # Rilascia l'autorizzazione sulla carta
        stripe_cancel_quietly(payment["payment_intent_id"])
        payment["state"] = PaymentState.CANCELLED
        payment["expired"] = True


@app.get("/health")
def health_check():
    return {
        "service": "payment-service",
        "status": "UP",
        "mode": "stripe-sandbox",
    }


@app.get("/state")
def get_state():
    return {
        "mode": "stripe-sandbox",
        "payments": payments,
    }


@app.put("/users/{username}")
def register_user(
    request: UserCardRequest,
    username: str = Path(pattern=r"^[a-z0-9_.-]{1,50}$"),
):
    """Crea (o aggiorna) il Customer Stripe dell'utente e la sua carta di test."""
    try:
        found = stripe.Customer.list(email=user_email(username), limit=1)
        if found.data:
            customer = stripe.Customer.modify(
                found.data[0].id,
                metadata={"test_payment_method": request.payment_method},
            )
        else:
            customer = stripe.Customer.create(
                email=user_email(username),
                name=username,
                metadata={
                    "username": username,
                    "test_payment_method": request.payment_method,
                },
            )
    except stripe.StripeError as error:
        raise stripe_http_error(error) from error

    user = customer_to_user(username, customer)
    user["payment_method"] = request.payment_method
    users[username] = user
    return user


@app.get("/users")
def list_users():
    return {name: user for name, user in users.items() if user is not None}


@app.post("/tcc/try")
def try_payment(request: PaymentTryRequest):
    expire_payment_if_needed(request.transaction_id)
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
            "payment_intent_id": existing["payment_intent_id"],
        }

    # Carta da usare: quella indicata nella richiesta, altrimenti quella
    # registrata per l'utente, altrimenti il default.
    user = load_user(request.username)
    payment_method = request.payment_method or (
        user["payment_method"] if user else STRIPE_DEFAULT_PAYMENT_METHOD
    )
    customer_params = {"customer": user["customer_id"]} if user else {}

    # TRY = autorizzazione senza addebito (capture_method=manual):
    # l'importo viene bloccato sulla carta ma non ancora incassato.
    try:
        intent = stripe.PaymentIntent.create(
            amount=to_minor_units(request.amount),
            currency=STRIPE_CURRENCY,
            payment_method=payment_method,
            **customer_params,
            capture_method="manual",
            confirm=True,
            automatic_payment_methods={
                "enabled": True,
                "allow_redirects": "never",
            },
            metadata={
                "transaction_id": request.transaction_id,
                "username": request.username,
            },
            # Stesso transaction_id => stessa richiesta Stripe,
            # nessuna doppia autorizzazione in caso di retry.
            idempotency_key=f"tcc-try-{request.transaction_id}",
        )
    except stripe.StripeError as error:
        raise stripe_http_error(error) from error

    if intent.status != "requires_capture":
        stripe_cancel_quietly(intent.id)
        raise HTTPException(
            status_code=409,
            detail=f"Autorizzazione non completata (stato: {intent.status})",
        )

    payments[request.transaction_id] = {
        "transaction_id": request.transaction_id,
        "username": request.username,
        "amount": request.amount,
        "payment_intent_id": intent.id,
        "payment_method": payment_method,
        "state": PaymentState.RESERVED,
        "expires_at": time.time() + TTL_SECONDS,
        "expired": False,
    }

    return {
        "status": "RESERVED",
        "transaction_id": request.transaction_id,
        "state": PaymentState.RESERVED,
        "payment_intent_id": intent.id,
    }


@app.put("/tcc/confirm")
def confirm_payment(request: TransactionRequest):
    expire_payment_if_needed(request.transaction_id)
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
        if payment.get("expired"):
            raise HTTPException(
                status_code=409,
                detail="Prenotazione scaduta"
            )
        raise HTTPException(
            status_code=409,
            detail="Pagamento già annullato",
        )

    # CONFIRM = capture dell'importo autorizzato
    try:
        stripe.PaymentIntent.capture(
            payment["payment_intent_id"],
            idempotency_key=f"tcc-confirm-{request.transaction_id}",
        )
    except stripe.StripeError as error:
        # Capture già avvenuta (es. risposta persa al giro prima)?
        try:
            intent = stripe.PaymentIntent.retrieve(
                payment["payment_intent_id"]
            )
            if intent.status == "succeeded":
                payment["state"] = PaymentState.CONFIRMED
                return {
                    "status": "ALREADY_CONFIRMED",
                    "transaction_id": request.transaction_id,
                    "state": PaymentState.CONFIRMED,
                }
        except stripe.StripeError:
            pass
        raise stripe_http_error(error) from error

    payment["state"] = PaymentState.CONFIRMED

    return {
        "status": "CONFIRMED",
        "transaction_id": request.transaction_id,
        "state": PaymentState.CONFIRMED,
        "payment_intent_id": payment["payment_intent_id"],
    }


@app.put("/tcc/cancel")
def cancel_payment(request: TransactionRequest):
    expire_payment_if_needed(request.transaction_id)
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

    # CANCEL = annulla il PaymentIntent e rilascia i fondi bloccati
    try:
        stripe.PaymentIntent.cancel(payment["payment_intent_id"])
    except stripe.StripeError as error:
        raise stripe_http_error(error) from error

    payment["state"] = PaymentState.CANCELLED

    return {
        "status": "CANCELLED",
        "transaction_id": request.transaction_id,
        "state": PaymentState.CANCELLED,
    }