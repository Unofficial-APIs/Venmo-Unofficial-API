import aiohttp
import asyncio
from typing import Optional, Dict, Any
from fake_useragent import UserAgent

from submodule_integrations.utils.errors import (
    IntegrationAuthError,
    IntegrationAPIError,
)
from submodule_integrations.models.integration import Integration

get_wallet_query = """
query getUserFundingInstruments {
  profile {
    ... on Profile {
      identity {
        ... on Identity {
          capabilities
          __typename
        }
        __typename
      }
      wallet {
        id
        assets {
          logoThumbnail
          __typename
        }
        instrumentType
        name
        fees {
          feeType
          fixedAmount
          variablePercentage
          __typename
        }
        metadata {
          ...BalanceMetadata
          ... on BankFundingInstrumentMetadata {
            bankName
            isVerified
            lastFourDigits
            uniqueIdentifier
            __typename
          }
          ... on CardFundingInstrumentMetadata {
            issuerName
            lastFourDigits
            networkName
            isVenmoCard
            expirationStatus
            quasiCash
            __typename
          }
          __typename
        }
        roles {
          merchantPayments
          peerPayments
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}

fragment BalanceMetadata on BalanceFundingInstrumentMetadata {
  availableBalance {
    value
    transactionType
    displayString
    __typename
  }
  __typename
}
"""

get_handle_query = """
query Identity {
  profile {
    ... on Profile {
      availableIdentities {
        ... on BusinessIdentity {
          handle
          type
        }
        ... on Identity {
          handle
          type
        }
      }
    }
  }
}
"""


class VenmoIntegration(Integration):
    def __init__(self, user_agent: str = UserAgent().random):
        super().__init__("venmo")
        self.url = "https://api.venmo.com/v1"
        self.identityJson = None
        self.transactionJson = None
        self.user_agent = user_agent
        self.is_limited_account = None

    async def _make_request(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        """Helper method to handle network requests using either custom requester or aiohttp"""
        if self.network_requester:
            response = await self.network_requester.request(
                method, url, process_response=self._handle_response, **kwargs
            )
            return response
        else:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, **kwargs) as response:
                    return await self._handle_response(response)

    async def initialize(self, authorization_token, network_requester=None):
        self.authorization_token = authorization_token
        self.headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.authorization_token}",
        }
        self.network_requester = network_requester
        self.identityJson = await self.get_identity()
        self.transactionJson = await self.get_personal_transaction()

    async def _handle_response(
        self, response: aiohttp.ClientResponse
    ) -> Dict[str, Any]:
        if response.status == 200:
            return await response.json()

        response_json = await response.json()
        error_message = response_json.get("error", {}).get("message", "Unknown error")
        error_code = response_json.get("error", {}).get("code", str(response.status))

        if response.status == 401:
            raise IntegrationAuthError(
                f"Venmo: {error_message}", response.status, error_code
            )
        elif response.status == 400 and error_message == "Resource not found.":
            raise IntegrationAPIError(
                self.integration_name,
                f"Resource not found.",
                response.status,
                error_code,
            )
        else:
            raise IntegrationAPIError(
                self.integration_name,
                f"{error_message} (HTTP {response.status})",
                response.status,
                error_code,
            )

    async def get_identity(self) -> Dict[str, Any]:
        """Gets the identity of the current account"""
        api_url = self.url + "/account"
        data = await self._make_request("GET", api_url, headers=self.headers)
        self.is_limited_account = self.safe_get(
            data, ["data", "is_limited_account"], "get_identity"
        )
        return data

    async def get_balance(self):
        return self.safe_get(self.identityJson, ["data", "balance"], "get_balance")

    async def get_personal_transaction(self) -> Dict[str, Any]:
        """Gets the list of all personal transactions"""
        api_url = (
            self.url
            + "/stories/target-or-actor/"
            + self.safe_get(
                self.identityJson, ["data", "user", "id"], "get_personal_transaction"
            )
        )
        return await self._make_request("GET", api_url, headers=self.headers)

    async def get_payment_methods(self, amount) -> Optional[Dict[str, Any]]:
        """Gets the user's payment methods and checks if Venmo balance is enough"""
        payload = {"query": get_wallet_query}
        data = await self._make_request(
            "POST", "https://api.venmo.com/graphql", headers=self.headers, json=payload
        )
        payment_methods = self.safe_get(
            data, ["data", "profile", "wallet"], "get_payment_methods"
        )

        primary_id = None
        backup_id = None
        double_backup_id = None

        for payment_method in payment_methods:
            peer_payments_role = self.safe_get(
                payment_method, ["roles", "peerPayments"], "get_payment_methods"
            )

            # Check primary payment method (if account is not limited)
            if peer_payments_role == "primary" and not self.is_limited_account:
                available_balance = self.safe_get(
                    payment_method,
                    ["metadata", "availableBalance", "value"],
                    "get_payment_methods",
                )
                if available_balance >= amount:
                    primary_id = self.safe_get(
                        payment_method, ["id"], "get_payment_methods"
                    )

            # Store backup payment method
            elif peer_payments_role == "backup":
                backup_id = self.safe_get(payment_method, ["id"], "get_payment_methods")

            # Store other active payment methods
            elif (
                peer_payments_role == "none"
                and payment_method.get("metadata", {}).get("expirationStatus")
                == "active"
            ):
                double_backup_id = self.safe_get(
                    payment_method, ["id"], "get_payment_methods"
                )

        # Return in priority order
        if primary_id:
            return primary_id
        if backup_id:
            return backup_id
        if double_backup_id:
            return double_backup_id

        return None

    async def get_user(self, user_id):
        """Gets the account ID of the specified user"""
        api_url = self.url + "/users/" + user_id
        return await self._make_request("GET", api_url, headers=self.headers)

    async def pay_user(self, user_id, amount, note, privacy="private") -> None:
        """Pays the user a certain amount of money"""
        api_url = self.url + "/payments"
        user_data = await self.get_user(user_id)
        recipient_id = self.safe_get(user_data, ["data", "id"], "pay_user")
        funding_source_id = await self.get_payment_methods(amount)
        if not funding_source_id:
            raise IntegrationAPIError(
                self.integration_name, f"No funding source available.", 500
            )

        body = {
            "funding_source_id": funding_source_id,
            "user_id": recipient_id,
            "audience": privacy,
            "amount": amount,
            "note": note,
        }

        return await self._make_request(
            "POST", api_url, headers=self.headers, json=body
        )

    async def request_user(self, user_id, amount, note, privacy="private") -> None:
        """Requests a certain amount of money from the user"""
        api_url = self.url + "/payments"
        recipient_id = self.safe_get(
            await self.get_user(user_id), ["data", "id"], "request_user"
        )

        body = {
            "user_id": recipient_id,
            "audience": privacy,
            "amount": -amount,
            "note": note,
        }

        return await self._make_request(
            "POST", api_url, headers=self.headers, json=body
        )

    async def get_handle(self):
        payload = {
            "operationName": "Identity",
            "variables": {},
            "query": get_handle_query,
        }
        data = await self._make_request(
            "POST", "https://api.venmo.com/graphql", headers=self.headers, json=payload
        )
        available_identities = self.safe_get(
            data, ["data", "profile", "availableIdentities"], "get_handle"
        )
        return available_identities


async def main():
    venmo = VenmoIntegration("Bearer YOUR_ACCESS_TOKEN")
    await venmo.initialize()
    print(await venmo.get_balance())
    await venmo.get_user("Alan-Lu-16")


if __name__ == "__main__":
    asyncio.run(main())
