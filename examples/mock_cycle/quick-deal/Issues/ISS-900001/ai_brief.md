# AI Brief - ISS-900001

- Title: Checkout button stays disabled after voucher apply
- Status: verified_closed
- Type: bug
- Updated At: 2026-03-05T01:15:00Z

## Problem

Valid voucher flow recalculates totals but checkout CTA stays disabled for guest checkout.

## Repro Steps

1. Open cart as guest with 2 eligible items.
2. Apply voucher code SAVE10.
3. Observe totals update and voucher badge shown.
4. Try clicking Proceed to Payment.

## Expected vs Actual

- Expected: Checkout button should be enabled after successful voucher apply and valid cart.
- Actual: Checkout button remains disabled; cannot continue to payment.

## Acceptance Criteria

1. Button enables within 500ms after voucher success response.
2. Unit test covers state transition after voucher apply action.
3. E2E test verifies guest voucher checkout can proceed.

## Latest Update

- Actor: QA Nina
- Time: 2026-03-05T01:15:00Z
- Status: fixed_pending_verify -> verified_closed
- Note: Retested on staging and production canary. Behavior is correct now.

