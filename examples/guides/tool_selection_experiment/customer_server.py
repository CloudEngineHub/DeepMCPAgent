"""Acme billing and support API: 4 resources x 5 standard tools + 6 actions = 26 tools.

Only used through all_in_one_server.py, to show what happens past 128 tools.
"""
from promptise.mcp.server import MCPServer

from acme_tools import add_tools

server = MCPServer("acme-customers")

RESOURCES = {
    "customer": ("customers", "cus_310", "a company that pays for Acme",
                 {"name": "Company name.", "billing_email": "Where invoices go."}),
    "subscription": ("subscriptions", "sub_77", "a customer's paid plan and its renewal date",
                     {"customer_id": "The customer.", "plan": "starter, team or business."}),
    "invoice": ("invoices", "inv_2041", "a bill sent to a customer",
                {"customer_id": "The customer.", "amount": "Amount in USD."}),
    "ticket": ("support tickets", "tk_918", "a customer's support request",
               {"customer_id": "The customer.", "subject": "Short subject line."}),
}
ACTIONS = {
    "refund_invoice": ("Refund all or part of a paid invoice to the customer's card.",
                       {"invoice_id": "The invoice.", "amount": "Optional amount; the full invoice by default."}),
    "apply_coupon": ("Apply a discount coupon to a customer's subscription.",
                     {"subscription_id": "The subscription.", "coupon": "Coupon code."}),
    "pause_subscription": ("Pause a subscription for up to three months. Nothing is billed while paused.",
                           {"subscription_id": "The subscription.", "months": "1, 2 or 3."}),
    "cancel_subscription": ("Cancel a subscription at the end of the current billing period.",
                            {"subscription_id": "The subscription."}),
    "add_ticket_note": ("Add an internal note to a support ticket. The customer doesn't see it.",
                        {"ticket_id": "The ticket.", "note": "Text of the note."}),
    "escalate_ticket": ("Hand a support ticket to the engineering on-call queue.", {"ticket_id": "The ticket."}),
}
add_tools(server, RESOURCES, ACTIONS)

if __name__ == "__main__":
    server.run()
