"""
Stripe Webhook Handler

Processes Stripe webhook events for subscription management:
- checkout.session.completed: Activate new subscription + add credits
- invoice.paid: Renew subscription + add credits
- customer.subscription.deleted: Cancel subscription
"""

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

import stripe
from fastapi import APIRouter, HTTPException, Request, status

from app.core.config import settings
from app.services.billing_service import get_billing_service, invalidate_balance_cache

logger = logging.getLogger(__name__)

router = APIRouter()


def _sget(obj, key, default=None):
    """
    Safely get an attribute from a Stripe object or dict.

    Stripe SDK returns StripeObject instances which support attribute access
    but NOT .get() as a method. This helper handles both cases.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _to_dict(obj):
    """Convert a Stripe object to a plain dict for safe nested access."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__iter__"):
        return dict(obj)
    return {}


@router.post("/stripe")
async def stripe_webhook(request: Request):
    """
    Receive and process Stripe webhook events.

    Security: Validates webhook signature using STRIPE_WEBHOOK_SECRET.
    Idempotency: Uses stripe_payment_id to prevent duplicate processing.
    """
    # Get raw body for signature verification
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not sig_header:
        logger.warning("[Stripe Webhook] Missing stripe-signature header")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing stripe-signature header"
        )

    if not settings.STRIPE_WEBHOOK_SECRET:
        logger.error("[Stripe Webhook] STRIPE_WEBHOOK_SECRET not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook secret not configured"
        )

    # Verify webhook signature
    try:
        stripe.api_key = settings.STRIPE_SECRET_KEY
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        logger.error(f"[Stripe Webhook] Invalid payload: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid payload"
        ) from e
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"[Stripe Webhook] Invalid signature: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid signature"
        ) from e

    event_type = event["type"]
    logger.info(f"[Stripe Webhook] Received event: {event_type}")

    billing_service = get_billing_service()

    try:
        # Handle different event types
        if event_type == "checkout.session.completed":
            await handle_checkout_completed(event, billing_service)

        elif event_type == "invoice.paid":
            await handle_invoice_paid(event, billing_service)

        elif event_type == "invoice.payment_failed":
            await handle_invoice_payment_failed(event, billing_service)

        elif event_type == "customer.subscription.deleted":
            await handle_subscription_deleted(event, billing_service)

        elif event_type == "customer.subscription.updated":
            await handle_subscription_updated(event, billing_service)

        else:
            logger.debug(f"[Stripe Webhook] Unhandled event type: {event_type}")

        return {"status": "success", "event_type": event_type}

    except Exception as e:
        logger.error(f"[Stripe Webhook] Error processing {event_type}: {e}", exc_info=True)
        # Return 500 so Stripe will retry
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing webhook: {str(e)}"
        ) from e


async def handle_checkout_completed(event: dict, billing_service):
    """
    Handle checkout.session.completed event.

    Per Stripe best practices:
    - This event is for linking customer_id and creating the subscription record
    - Do NOT provision access/credits here
    - Credits are added when invoice.paid is received
    """
    session = event["data"]["object"]

    # Extract metadata (we pass company_id and plan_id during checkout creation)
    metadata = _to_dict(_sget(session, "metadata", {}))
    company_id = metadata.get("company_id")
    plan_id = metadata.get("plan_id")

    # Top-up branch (F15): one-time credit purchases use mode="payment" with
    # metadata.type=="topup" and NO plan_id (stripe_checkout.create_topup_checkout).
    # Branch BEFORE the plan_id requirement so a top-up does not raise ValueError
    # → 500 → infinite Stripe retry. Idempotency is guaranteed by F14 (UNIQUE on
    # stripe_payment_id + add_credits insert-first), so a duplicate delivery of
    # the same checkout.session.completed credits exactly once.
    if _sget(session, "mode") == "payment" or metadata.get("type") == "topup":
        await handle_topup_completed(session, metadata, billing_service)
        return

    if not company_id or not plan_id:
        logger.error(f"[Stripe Webhook] checkout.session.completed missing metadata: {metadata}")
        raise ValueError("Missing company_id or plan_id in metadata")

    # Get subscription details
    stripe_subscription_id = _sget(session, "subscription")
    stripe_customer_id = _sget(session, "customer")

    if not stripe_subscription_id:
        logger.warning("[Stripe Webhook] No subscription in session (one-time payment?)")
        return

    # Fetch subscription from Stripe to get period dates
    subscription = stripe.Subscription.retrieve(stripe_subscription_id)

    try:
        items = _sget(subscription, "items")
        items_data = _sget(items, "data", []) if items else []
        if items_data and len(items_data) > 0:
            first_item = items_data[0]
            period_start = _sget(first_item, "current_period_start", 0)
            period_end = _sget(first_item, "current_period_end", 0)
        else:
            period_start = _sget(subscription, "created", 0)
            period_end = _sget(subscription, "created", 0) + (30 * 24 * 60 * 60)

        current_period_start = datetime.fromtimestamp(int(period_start)) if period_start else datetime.utcnow()
        current_period_end = datetime.fromtimestamp(int(period_end)) if period_end else datetime.utcnow()
    except Exception as e:
        logger.warning(f"[Stripe Webhook] Error extracting period dates: {e}, using defaults")
        current_period_start = datetime.utcnow()
        current_period_end = datetime.utcnow()

    logger.info(f"[Stripe Webhook] Creating subscription record for company {company_id}, plan {plan_id} (credits via invoice.paid)")

    # Create subscription record WITHOUT adding credits
    # Credits will be added by invoice.paid event
    success = billing_service.setup_subscription(
        company_id=company_id,
        plan_id=plan_id,
        stripe_subscription_id=stripe_subscription_id,
        stripe_customer_id=stripe_customer_id,
        current_period_start=current_period_start,
        current_period_end=current_period_end
    )

    if success:
        invalidate_balance_cache(company_id)
        logger.info(f"[Stripe Webhook] ✅ Subscription record created for company {company_id}")
    else:
        raise ValueError(f"Failed to setup subscription for company {company_id}")


async def handle_topup_completed(session, metadata: dict, billing_service):
    """
    Handle a one-time top-up checkout (mode="payment", metadata.type="topup").

    Top-ups carry no plan_id — they only need company_id + amount_brl (both set
    by create_topup_checkout). Credits are granted via add_credits, which is
    idempotent under Stripe's at-least-once delivery thanks to F14 (UNIQUE on
    stripe_payment_id + insert-first), so a duplicate event credits exactly once.
    """
    company_id = metadata.get("company_id")
    amount_brl_raw = metadata.get("amount_brl")

    if not company_id or amount_brl_raw is None or str(amount_brl_raw) == "":
        logger.error(f"[Stripe Webhook] top-up missing company_id/amount_brl in metadata: {metadata}")
        raise ValueError("Missing company_id or amount_brl in top-up metadata")

    try:
        amount_brl = Decimal(str(amount_brl_raw))
    except (InvalidOperation, ValueError) as e:
        # amount_brl is set by our own checkout endpoint, but guard against a
        # malformed value so we don't 500 → trigger an infinite Stripe retry.
        logger.error(f"[Stripe Webhook] top-up invalid amount_brl={amount_brl_raw!r}: {e}")
        raise ValueError(f"Invalid amount_brl in top-up metadata: {amount_brl_raw!r}") from e

    # Idempotency key: prefer the PaymentIntent; fall back to the session id when
    # the PaymentIntent is not expanded on the session object.
    stripe_payment_id = _sget(session, "payment_intent") or _sget(session, "id")

    logger.info(f"[Stripe Webhook] Processing top-up for company {company_id}: R${amount_brl} (payment={stripe_payment_id})")

    success = billing_service.add_credits(
        company_id=company_id,
        amount_brl=amount_brl,
        transaction_type="topup",
        description="Recarga de créditos",
        stripe_payment_id=stripe_payment_id
    )

    if success:
        invalidate_balance_cache(company_id)
        logger.info(f"[Stripe Webhook] ✅ Top-up credited for company {company_id}: R${amount_brl}")
    else:
        raise ValueError(f"Failed to credit top-up for company {company_id}")


async def handle_invoice_paid(event: dict, billing_service):
    """
    Handle invoice.paid event.

    Per Stripe best practices:
    - This is THE event for provisioning access/credits
    - Handles ALL billing_reasons: subscription_create, subscription_cycle, subscription_update
    """
    invoice = event["data"]["object"]

    billing_reason = _sget(invoice, "billing_reason")

    # In Stripe API 2025+, the subscription field is deprecated
    # New location: invoice.parent.subscription_details.subscription
    stripe_subscription_id = _sget(invoice, "subscription")  # Legacy field

    # Check new API location: parent.subscription_details.subscription
    if not stripe_subscription_id:
        parent = _sget(invoice, "parent")
        if _sget(parent, "type") == "subscription_details":
            sub_details = _sget(parent, "subscription_details")
            stripe_subscription_id = _sget(sub_details, "subscription")

    # Fallback: check lines.data[0].subscription
    if not stripe_subscription_id:
        lines = _sget(invoice, "lines")
        lines_data = _sget(lines, "data", []) if lines else []
        if lines_data:
            stripe_subscription_id = _sget(lines_data[0], "subscription")
            # Also try lines.data[0].parent
            if not stripe_subscription_id:
                line_parent = _sget(lines_data[0], "parent")
                if _sget(line_parent, "type") == "subscription_item_details":
                    sub_item_details = _sget(line_parent, "subscription_item_details")
                    stripe_subscription_id = _sget(sub_item_details, "subscription")

    logger.info(f"[Stripe Webhook] invoice.paid: billing_reason={billing_reason}, subscription={stripe_subscription_id}")

    if not stripe_subscription_id:
        logger.info("[Stripe Webhook] Invoice not related to subscription, skipping")
        return

    # Process ALL subscription-related invoices
    if billing_reason not in ["subscription_create", "subscription_cycle", "subscription_update"]:
        logger.info(f"[Stripe Webhook] Unhandled billing_reason: {billing_reason}, skipping")
        return

    # Use invoice ID as payment identifier for idempotency
    stripe_payment_id = _sget(invoice, "id")

    # Check idempotency first
    if billing_service.is_payment_processed(stripe_payment_id):
        logger.info(f"[Stripe Webhook] Invoice {stripe_payment_id} already processed, skipping")
        return

    # Extract the REAL amount paid (converting from centavos to BRL)
    amount_paid_cents = _sget(invoice, "amount_paid", 0)
    amount_paid_brl = Decimal(str(amount_paid_cents)) / Decimal("100")

    logger.info(f"[Stripe Webhook] Processing invoice: reason={billing_reason}, amount=R${amount_paid_brl}")

    # Fetch subscription from Stripe to get updated period dates
    subscription = stripe.Subscription.retrieve(stripe_subscription_id)

    try:
        items = _sget(subscription, "items")
        items_data = _sget(items, "data", []) if items else []
        if items_data and len(items_data) > 0:
            first_item = items_data[0]
            period_start = _sget(first_item, "current_period_start", 0)
            period_end = _sget(first_item, "current_period_end", 0)
        else:
            period_start = _sget(subscription, "created", 0)
            period_end = _sget(subscription, "created", 0) + (30 * 24 * 60 * 60)

        current_period_start = datetime.fromtimestamp(int(period_start)) if period_start else datetime.utcnow()
        current_period_end = datetime.fromtimestamp(int(period_end)) if period_end else datetime.utcnow()
    except Exception as e:
        logger.warning(f"[Stripe Webhook] Error extracting period dates: {e}, using defaults")
        current_period_start = datetime.utcnow()
        current_period_end = datetime.utcnow()

    # For initial subscription, get company_id from subscription metadata or lookup
    company_id = None
    plan_id = None
    subscription_exists = False

    # Try to get from existing subscription record first
    sub_info = billing_service.get_subscription_by_stripe_id(stripe_subscription_id)
    if sub_info:
        company_id = sub_info.get("company_id")
        plan_id = sub_info.get("plan_id")
        subscription_exists = True

    if not company_id:
        # Subscription record doesn't exist yet (invoice.paid arrived before checkout.session.completed)
        # Get from subscription metadata
        sub_metadata = _to_dict(_sget(subscription, "metadata", {}))
        company_id = sub_metadata.get("company_id")
        plan_id = sub_metadata.get("plan_id")

    if not company_id or not plan_id:
        logger.error(f"[Stripe Webhook] Cannot find company_id/plan_id for subscription {stripe_subscription_id}")
        raise ValueError(f"Cannot find company_id/plan_id for subscription {stripe_subscription_id}")

    # If subscription record doesn't exist, create it first
    # This handles the race condition where invoice.paid arrives before checkout.session.completed
    if not subscription_exists:
        logger.info("[Stripe Webhook] Creating subscription record (invoice.paid arrived first)")
        stripe_customer_id = _sget(subscription, "customer")
        billing_service.setup_subscription(
            company_id=company_id,
            plan_id=plan_id,
            stripe_subscription_id=stripe_subscription_id,
            stripe_customer_id=stripe_customer_id,
            current_period_start=current_period_start,
            current_period_end=current_period_end
        )

    # Add credits and update subscription
    success = billing_service.process_invoice_payment(
        stripe_subscription_id=stripe_subscription_id,
        stripe_payment_id=stripe_payment_id,
        amount_paid=amount_paid_brl,
        billing_reason=billing_reason,
        current_period_start=current_period_start,
        current_period_end=current_period_end
    )

    if success:
        invalidate_balance_cache(company_id)
        reason_label = {
            "subscription_create": "activated",
            "subscription_cycle": "renewed",
            "subscription_update": "updated"
        }.get(billing_reason, billing_reason)
        logger.info(f"[Stripe Webhook] ✅ Subscription {reason_label}: {stripe_subscription_id}")
    else:
        raise ValueError(f"Failed to process invoice {stripe_payment_id}")


async def handle_invoice_payment_failed(event: dict, billing_service):
    """
    Handle invoice.payment_failed event.

    Marks subscription as 'past_due' so frontend can display a warning banner.
    """
    invoice = event["data"]["object"]

    billing_reason = _sget(invoice, "billing_reason")
    invoice_id = _sget(invoice, "id")
    customer_email = _sget(invoice, "customer_email")

    logger.warning(f"[Stripe Webhook] 💳 PAYMENT FAILED: invoice={invoice_id}, email={customer_email}, reason={billing_reason}")

    # Get subscription ID using same logic as invoice.paid
    stripe_subscription_id = _sget(invoice, "subscription")
    logger.info(f"[Stripe Webhook] Step 1: invoice.subscription = {stripe_subscription_id}")

    if not stripe_subscription_id:
        parent = _sget(invoice, "parent")
        parent_type = _sget(parent, "type")
        logger.info(f"[Stripe Webhook] Step 2: parent.type = {parent_type}")

        if parent_type == "subscription_details":
            sub_details = _sget(parent, "subscription_details")
            stripe_subscription_id = _sget(sub_details, "subscription")
            logger.info(f"[Stripe Webhook] Step 2b: parent.subscription_details.subscription = {stripe_subscription_id}")

    if not stripe_subscription_id:
        lines = _sget(invoice, "lines")
        lines_data = _sget(lines, "data", []) if lines else []
        logger.info(f"[Stripe Webhook] Step 3: lines.data count = {len(lines_data)}")

        if lines_data:
            # Old API: lines.data[0].subscription
            stripe_subscription_id = _sget(lines_data[0], "subscription")
            logger.info(f"[Stripe Webhook] Step 3b: lines.data[0].subscription = {stripe_subscription_id}")

            if not stripe_subscription_id:
                line_parent = _sget(lines_data[0], "parent")
                line_parent_type = _sget(line_parent, "type")
                logger.info(f"[Stripe Webhook] Step 3c: lines.data[0].parent.type = {line_parent_type}")

                if line_parent_type == "subscription_item_details":
                    sub_item_details = _sget(line_parent, "subscription_item_details")
                    stripe_subscription_id = _sget(sub_item_details, "subscription")
                    logger.info(f"[Stripe Webhook] Step 3d: from subscription_item_details = {stripe_subscription_id}")

    if not stripe_subscription_id:
        logger.error(f"[Stripe Webhook] ❌ FAILED TO EXTRACT SUBSCRIPTION ID from invoice {invoice_id}!")
        logger.info(f"[Stripe Webhook] Full invoice keys: {list(invoice.keys()) if hasattr(invoice, 'keys') else 'N/A'}")
        logger.info(f"[Stripe Webhook] invoice.parent = {_sget(invoice, 'parent')}")
        return

    logger.warning(f"[Stripe Webhook] ⚠️ Payment failed for subscription {stripe_subscription_id}")

    # Mark subscription as past_due
    success = billing_service.mark_subscription_past_due(stripe_subscription_id)

    if success:
        logger.info(f"[Stripe Webhook] ✅ Subscription {stripe_subscription_id} marked as past_due")
    else:
        logger.error(f"[Stripe Webhook] ❌ Failed to mark subscription {stripe_subscription_id} as past_due - check if subscription exists in database")


async def handle_subscription_deleted(event: dict, billing_service):
    """
    Handle customer.subscription.deleted event.

    This is triggered when a subscription is canceled (immediately or at period end).
    """
    subscription = event["data"]["object"]
    stripe_subscription_id = _sget(subscription, "id")

    if not stripe_subscription_id:
        logger.error("[Stripe Webhook] subscription.deleted missing subscription ID")
        return

    success = billing_service.cancel_subscription(stripe_subscription_id)

    if success:
        logger.info(f"[Stripe Webhook] ✅ Subscription cancelled: {stripe_subscription_id}")
    else:
        logger.warning(f"[Stripe Webhook] Subscription not found for cancellation: {stripe_subscription_id}")


async def handle_subscription_updated(event: dict, billing_service):
    """
    Handle customer.subscription.updated event.

    - cancel_at has value: subscription is scheduled for cancellation
    - cancel_at is null: subscription active (or cancellation was reverted)
    """
    subscription = event["data"]["object"]
    stripe_subscription_id = _sget(subscription, "id")

    if not stripe_subscription_id:
        logger.error("[Stripe Webhook] subscription.updated missing subscription ID")
        return

    # cancel_at: Unix timestamp if scheduled, None if active
    cancel_at = _sget(subscription, "cancel_at")

    if cancel_at:
        logger.info(f"[Stripe Webhook] Subscription {stripe_subscription_id} scheduled to cancel at {cancel_at}")
    else:
        logger.info(f"[Stripe Webhook] Subscription {stripe_subscription_id} is active (no cancellation)")

    # Update cancel_at in database (null = no cancellation, timestamp = scheduled)
    billing_service.update_subscription_cancel_at(
        stripe_subscription_id=stripe_subscription_id,
        cancel_at=cancel_at
    )

    # Check for plan change
    items = _sget(subscription, "items")
    items_data = _sget(items, "data", []) if items else []
    if not items_data:
        return

    first_item = items_data[0]
    price = _sget(first_item, "price")
    new_price_id = _sget(price, "id") if price else None
    if not new_price_id:
        return

    logger.info(f"[Stripe Webhook] Subscription updated: {stripe_subscription_id}, new price: {new_price_id}")

    billing_service.update_subscription_plan_by_price(
        stripe_subscription_id=stripe_subscription_id,
        stripe_price_id=new_price_id
    )
