"""A deliberately broken customer lookup agent. All data is local and synthetic."""

CUSTOMERS = {
    "C001": {"id": "C001", "name": "Ada", "email": "ada@example.test"},
    "C002": {"id": "C002", "name": "Lin", "email": "lin@example.test"},
}


def tool_lookup(customer_id: str) -> dict:
    if not isinstance(customer_id, str) or not customer_id.startswith("C"):
        raise ValueError("customer_id must be a string beginning with C")
    return dict(CUSTOMERS[customer_id])


def lookup_customer(customer_id: str) -> dict:
    # Bug: the caller still uses the tool's obsolete argument name.
    arguments = {"user_id": customer_id}
    return tool_lookup(**arguments)


def lookup_customer_by_email(email: str) -> dict | None:
    for customer in CUSTOMERS.values():
        if customer["email"] == email:
            return dict(customer)
    return None
