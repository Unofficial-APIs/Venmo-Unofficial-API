import requests
from typing import Optional, Dict, Any
from fake_useragent import UserAgent

from utils.errors import IntegrationAuthError, IntegrationAPIError
from models.integration import Integration

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

class VenmoIntegration(Integration):
    def __init__(self, authorization_token, user_agent: str = UserAgent().random):
        super().__init__("venmo")
        self.authorization_token = authorization_token
        self.url = "https://api.venmo.com/v1"
        self.headers = {
            "User-Agent": user_agent,
            "Content-Type": "application/json",
            "Authorization": self.authorization_token,
        }
        self.identityJson = self.get_identity()
        self.transactionJson = self.get_personal_transaction()

    def _handle_response(self, response: requests.Response) -> Dict[str, Any]:
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 401:
            raise IntegrationAuthError("Invalid or expired token", response.status_code)
        else:
            raise IntegrationAPIError(self.integration_name, f"HTTP error occurred: {response.status_code}")

    def get_balance(self):
        return self.safe_get(self.identityJson, ["data", "balance"], "get_balance")

    def get_personal_transaction(self) -> Dict[str, Any]:
        """Gets the list of all personal transactions"""
        api_url = (
            self.url + "/stories/target-or-actor/" + 
            self.safe_get(self.identityJson, ["data", "user", "id"], "get_personal_transaction")
        )
        response = requests.get(headers=self.headers, url=api_url)
        return self._handle_response(response)

    def get_payment_methods(self, amount) -> Optional[Dict[str, Any]]:
        """Gets the user's payment methods and checks if Venmo balance is enough"""
        payload = {"query": get_wallet_query}
        response = requests.post(
            headers=self.headers, url="https://api.venmo.com/graphql", json=payload
        )
        data = self._handle_response(response)

        primary_payment = None
        backup_payment = None

        for payment_method in self.safe_get(data, ["data", "profile", "wallet"], "get_payment_methods"):
            if self.safe_get(payment_method, ["roles", "merchantPayments"], "get_payment_methods") == "primary":
                primary_payment = payment_method
            elif self.safe_get(payment_method, ["roles", "merchantPayments"], "get_payment_methods") == "backup":
                backup_payment = payment_method

        if primary_payment and self.safe_get(primary_payment, ["metadata", "availableBalance", "value"], "get_payment_methods") >= amount:
            return self.safe_get(primary_payment, ["id"], "get_payment_methods")

        if backup_payment:
            return self.safe_get(backup_payment, ["id"], "get_payment_methods")

        return None

    def get_user(self, user_id):
        """Gets the account ID of the specified user"""
        api_url = self.url + "/users/" + user_id
        response = requests.get(headers=self.headers, url=api_url)
        return self._handle_response(response)

    def pay_user(self, user_id, amount, note, privacy="private") -> None:
        """Pays the user a certain amount of money"""
        api_url = self.url + "/payments"
        recipient_id = self.safe_get(self.get_user(user_id), ["data", "id"], "pay_user")
        funding_source_id = self.get_payment_methods(amount)

        if not funding_source_id:
            raise ValueError("No funding sources available")

        body = {
            "funding_source_id": funding_source_id,
            "user_id": recipient_id,
            "audience": privacy,
            "amount": amount,
            "note": note,
        }

        response = requests.post(headers=self.headers, url=api_url, json=body)
        self._handle_response(response)
        print("Paid successfully!")

    def request_user(self, user_id, amount, note, privacy="private") -> None:
        """Requests a certain amount of money from the user"""
        api_url = self.url + "/payments"
        recipient_id = self.safe_get(self.get_user(user_id), ["data", "id"], "request_user")

        body = {
            "user_id": recipient_id,
            "audience": privacy,
            "amount": -amount,
            "note": note,
        }

        response = requests.post(headers=self.headers, url=api_url, json=body)
        self._handle_response(response)
        print("Request sent successfully!")


if __name__ == "__main__":
    venmo = VenmoIntegration("Bearer YOUR_ACCESS_TOKEN")
    print(venmo.get_balance())
    venmo.get_user("Alan-Lu-16")
