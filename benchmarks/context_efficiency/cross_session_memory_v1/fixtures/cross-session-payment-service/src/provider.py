from __future__ import annotations


class MockPaymentProvider:
    def charge(self, amount: str) -> dict[str, str]:
        return {"status": "fixture", "amount": amount}
