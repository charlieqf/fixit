# ISS-900001 - Checkout button stays disabled after voucher apply

## Metadata
- Reporter Name: QA Nina
- Issue Type: bug
- Status: verified_closed
- Created At (UTC): 2026-03-04T03:00:00Z
- Updated At (UTC): 2026-03-05T01:15:00Z

## Problem Summary

Valid voucher flow recalculates totals but checkout CTA stays disabled for guest checkout.

## Description

### Problem

When a user applies a valid voucher, totals recalculate but the `Proceed to Payment` button remains disabled.

![checkout-disabled](attachments/2026-03-04T03-00-00Z_embedded_1.png)

## Reproduction Steps

1. Open cart as guest with 2 eligible items.
2. Apply voucher code SAVE10.
3. Observe totals update and voucher badge shown.
4. Try clicking Proceed to Payment.

## Expected Result

Checkout button should be enabled after successful voucher apply and valid cart.

## Actual Result

Checkout button remains disabled; cannot continue to payment.

## Impact

All guest users using voucher checkout on web.

## Environment

web-staging, Chrome 122, build 2026.03.04.1, CentOS QA env

## Suspected Component

frontend/cart/checkoutButtonState

## Acceptance Criteria

1. Button enables within 500ms after voucher success response.
2. Unit test covers state transition after voucher apply action.
3. E2E test verifies guest voucher checkout can proceed.

## Initial Notes

Observed in staging after voucher rollout.

## Attachments

- [2026-03-04T03-00-00Z_frontend-console.log](attachments/2026-03-04T03-00-00Z_frontend-console.log) (uploaded)
- [2026-03-04T03-00-00Z_network.har.txt](attachments/2026-03-04T03-00-00Z_network.har.txt) (uploaded)
- [2026-03-04T03-00-00Z_embedded_1.png](attachments/2026-03-04T03-00-00Z_embedded_1.png) (embedded)

## Links

- Related Paths: frontend/src/features/cart/checkout.tsx, frontend/src/features/cart/useCheckoutState.ts
- Related Commits: 1a2b3c4
- Related Issues: ISS-899940

