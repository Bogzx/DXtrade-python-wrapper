"""Account discovery and read-only account data: balance, positions, orders, history."""

from typing import Any, List, Mapping, Optional, Tuple


from .exceptions import (
    DXTradeAPIError,
)
from .models import (
    Balance,
    Order,
    Position,
    account_code,
    parse_balance,
    parse_order,
    parse_position,
    unwrap_list,
)
from ._state import JSON
from ._session import _SessionLayer


class _AccountLayer(_SessionLayer):
    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    def _full_username(self) -> str:
        if "@" in self._username:
            return self._username
        return f"{self._username}@{self._domain_or_vendor}"

    def get_accounts(self) -> List[JSON]:
        """GET /users/{login@domain}: the user's accounts.

        Returns a list of dicts with ``account`` (code), ``base_currency``,
        ``status`` and ``position_based``.
        """
        self._check_authenticated()
        response = self._request(
            "GET", f"users/{self._seg(self._full_username())}", "Get user accounts"
        )
        users = unwrap_list(self._json(response, "Get user accounts"), "userDetails")
        accounts = []
        for user in users:
            for details in user.get("accounts") or []:
                code = account_code(details.get("account"))
                accounts.append({
                    "account": code,
                    "base_currency": details.get("baseCurrency"),
                    "status": details.get("accountStatus"),
                    "position_based": details.get("positionBased", details.get("isPositionBased")),
                })
                if code and details.get("baseCurrency"):
                    self._account_currency[code] = details["baseCurrency"]
        self._accounts_cache = accounts
        return accounts

    def _get_account_id(self) -> str:
        """The account code for account-scoped calls, discovering it if needed.

        Never invents one: with zero or several accounts it raises and says how
        to choose.
        """
        if self._account_id:
            return self._account_id
        known = self._accounts_cache if self._accounts_cache is not None else self.get_accounts()
        accounts = [a["account"] for a in known if a["account"]]
        if len(accounts) == 1:
            self._account_id = accounts[0]
            self._logger.info("Using account %s", self._account_id)
            return self._account_id
        if not accounts:
            raise DXTradeAPIError(
                "No account code available: the user has no accounts. Pass account=... "
                "(DXTRADE_ACCOUNT) with the code shown in your broker's web terminal."
            )
        raise DXTradeAPIError(
            f"User has {len(accounts)} accounts ({', '.join(accounts)}); pass account=... "
            "(DXTRADE_ACCOUNT) to choose one."
        )

    def _account_path(self, suffix: str) -> str:
        return f"accounts/{self._seg(self._get_account_id())}/{suffix}"

    # ------------------------------------------------------------------
    # Account data
    # ------------------------------------------------------------------

    def get_account_metrics(self, include_positions: bool = False) -> JSON:
        """GET /accounts/{account}/metrics: the raw AccountMetrics object."""
        self._check_authenticated()
        params = {"include-positions": "true"} if include_positions else None
        response = self._request("GET", self._account_path("metrics"), "Get account metrics",
                                 params=params)
        metrics = unwrap_list(self._json(response, "Get account metrics"), "metrics")
        account = self._get_account_id()
        for entry in metrics:
            if account_code(entry.get("account")) == account:
                return entry
        if len(metrics) == 1:
            return metrics[0]
        raise DXTradeAPIError(f"No metrics returned for account {account}")

    def get_balance(self) -> Balance:
        """Equity, balance, margin and P/L from GET /accounts/{account}/metrics."""
        metrics = self.get_account_metrics()
        return parse_balance(metrics, currency=self._account_currency.get(self._get_account_id()))

    def get_portfolio(self) -> JSON:
        """GET /accounts/{account}/portfolio: raw balances, positions and orders."""
        self._check_authenticated()
        response = self._request("GET", self._account_path("portfolio"), "Get portfolio")
        portfolios = unwrap_list(self._json(response, "Get portfolio"), "portfolios")
        if not portfolios:
            raise DXTradeAPIError("Portfolio response was empty")
        return portfolios[0]

    def get_positions(self, include_pnl: bool = False) -> List[Position]:
        """Open positions from GET /accounts/{account}/positions.

        Args:
            include_pnl: Also fetch per-position metrics and fill ``Position.pnl``
                with the floating P/L (``fpl``, account currency). One extra request.
        """
        self._check_authenticated()
        response = self._request("GET", self._account_path("positions"), "Get positions")
        positions = [parse_position(p)
                     for p in unwrap_list(self._json(response, "Get positions"), "positions")]
        if include_pnl and positions:
            metrics = self.get_account_metrics(include_positions=True)
            fpl = {str(m.get("positionCode")): m.get("fpl")
                   for m in metrics.get("positions") or [] if isinstance(m, Mapping)}
            for position in positions:
                value = fpl.get(position.position_id)
                if value is not None:
                    position.pnl = float(value)
        return positions

    def _open_orders_raw(self) -> Tuple[List[JSON], Optional[str]]:
        """Open orders plus the ETag the server sent with them (for If-Match)."""
        self._check_authenticated()
        response = self._request("GET", self._account_path("orders"), "Get orders")
        orders = unwrap_list(self._json(response, "Get orders"), "orders")
        return orders, response.headers.get("ETag")

    def get_orders(self) -> List[Order]:
        """Working orders from GET /accounts/{account}/orders."""
        orders, _ = self._open_orders_raw()
        return [parse_order(o) for o in orders]

    def get_order_history(self, limit: int = 100, **filters: Any) -> List[JSON]:
        """GET /accounts/{account}/orders/history.

        Args:
            limit: Maximum orders to return (the server may cap it).
            **filters: Any documented query filter, with underscores for dashes:
                ``in_status="COMPLETED"``, ``period="today"``, ``for_instrument="EUR/USD"``,
                ``transaction_to=...`` (for paging via nextPageTransactionTime).

        Returns:
            The raw ``Order`` dicts, most recent first.
        """
        self._check_authenticated()
        params = {"limit": limit}
        params.update({k.replace("_", "-"): v for k, v in filters.items()})
        response = self._request("GET", self._account_path("orders/history"),
                                 "Get order history", params=params)
        return unwrap_list(self._json(response, "Get order history"), "orders")
