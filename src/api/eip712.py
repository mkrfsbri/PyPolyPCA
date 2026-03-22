"""
EIP-712 Typed Structured Data Signing for Polymarket gasless orders.

Polymarket uses EIP-712 to sign orders that the relayer submits on-chain
without the trader paying gas.  The domain and type schema match the
deployed CTFExchange contract on Polygon (chain_id=137).

Reference:
  https://eips.ethereum.org/EIPS/eip-712
  https://github.com/Polymarket/py-clob-client
"""
from __future__ import annotations

import hashlib
import logging
import struct
from typing import Any, Dict, Optional, Tuple

from eth_account import Account  # type: ignore
from eth_account.messages import encode_typed_data  # type: ignore
from eth_typing import HexStr  # type: ignore
from web3 import Web3  # type: ignore

import config as cfg

logger = logging.getLogger("polybot.eip712")

# ---------------------------------------------------------------------------
# EIP-712 Domain for Polymarket CTFExchange on Polygon
# ---------------------------------------------------------------------------
DOMAIN = {
    "name": "ClobAuthDomain",
    "version": "1",
    "chainId": 137,
}

# Order type hash as used by CTFExchange
_ORDER_TYPE_FIELDS = [
    {"name": "salt",        "type": "uint256"},
    {"name": "maker",       "type": "address"},
    {"name": "signer",      "type": "address"},
    {"name": "taker",       "type": "address"},
    {"name": "tokenId",     "type": "uint256"},
    {"name": "makerAmount", "type": "uint256"},
    {"name": "takerAmount", "type": "uint256"},
    {"name": "expiration",  "type": "uint256"},
    {"name": "nonce",       "type": "uint256"},
    {"name": "feeRateBps",  "type": "uint256"},
    {"name": "side",        "type": "uint8"},
    {"name": "signatureType", "type": "uint8"},
]

TYPED_DATA_TYPES = {
    "EIP712Domain": [
        {"name": "name",    "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "Order": _ORDER_TYPE_FIELDS,
}


class ProxyWalletSigner:
    """
    Signs Polymarket orders using the Proxy Wallet (Funder) pattern.

    When a Funder Address is configured:
      - The *maker* field is set to the funder/proxy address.
      - The *signer* field is set to the EOA that holds the private key.
      - This allows the relayer to execute gasless fills on behalf of the funder.

    When no funder is set, maker == signer (standard EOA signing).
    """

    def __init__(
        self,
        private_key: str = cfg.PRIVATE_KEY,
        funder_address: str = cfg.FUNDER_ADDRESS,
    ) -> None:
        if not private_key:
            raise ValueError("POLY_PRIVATE_KEY environment variable is not set")
        self._account = Account.from_key(private_key)
        self.signer_address: str = self._account.address
        self.maker_address: str = (
            Web3.to_checksum_address(funder_address)
            if funder_address
            else self.signer_address
        )
        logger.info(
            "ProxyWalletSigner ready | signer=%s | maker=%s",
            self.signer_address[:10] + "…",
            self.maker_address[:10] + "…",
        )

    def sign_order(self, order_data: Dict[str, Any]) -> Tuple[str, str]:
        """
        Sign an order dict using EIP-712 and return (signature_hex, sig_type).

        sig_type:
          "0x01"  – POLY_PROXY   (funder != signer)
          "0x02"  – POLY_GNOSIS  (reserved; not used here)
          "0x00"  – EOA          (funder == signer)
        """
        # Build the full typed-data payload
        structured = {
            "domain": DOMAIN,
            "types": TYPED_DATA_TYPES,
            "primaryType": "Order",
            "message": {
                "salt":          int(order_data.get("salt", 0)),
                "maker":         self.maker_address,
                "signer":        self.signer_address,
                "taker":         order_data.get("taker", "0x0000000000000000000000000000000000000000"),
                "tokenId":       int(order_data.get("token_id", 0)),
                "makerAmount":   int(order_data.get("maker_amount", 0)),
                "takerAmount":   int(order_data.get("taker_amount", 0)),
                "expiration":    int(order_data.get("expiration", 0)),
                "nonce":         int(order_data.get("nonce", 0)),
                "feeRateBps":    int(order_data.get("fee_rate_bps", 0)),
                "side":          int(order_data.get("side", 0)),   # 0=BUY 1=SELL
                "signatureType": int(order_data.get("signature_type", 0)),
            },
        }

        signable_message = encode_typed_data(full_message=structured)
        signed = self._account.sign_message(signable_message)
        sig_hex: str = signed.signature.hex()

        if self.maker_address.lower() != self.signer_address.lower():
            sig_type = "0x01"  # POLY_PROXY
        else:
            sig_type = "0x00"  # EOA

        logger.debug("Order signed | type=%s | sig=%s…", sig_type, sig_hex[:16])
        return sig_hex, sig_type

    def sign_api_key_request(self, nonce: int, timestamp: int) -> str:
        """
        Sign the CLOB API key creation / L2 auth request.
        Returns the hex signature.
        """
        body = f"{timestamp}{nonce}"
        msg = encode_typed_data(full_message={
            "domain": DOMAIN,
            "types": {
                "EIP712Domain": TYPED_DATA_TYPES["EIP712Domain"],
                "ClobAuth": [
                    {"name": "address",   "type": "address"},
                    {"name": "timestamp", "type": "string"},
                    {"name": "nonce",     "type": "uint256"},
                    {"name": "message",   "type": "string"},
                ],
            },
            "primaryType": "ClobAuth",
            "message": {
                "address":   self.signer_address,
                "timestamp": str(timestamp),
                "nonce":     nonce,
                "message":   "This message attests that I control the given wallet",
            },
        })
        signed = self._account.sign_message(msg)
        return signed.signature.hex()
