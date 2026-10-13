from smolagents import local_python_executor as executor


def run(codes):
    """Execute known small agent code steps in one session.

    Each step returns its own print output, result and error flag. Earlier user
    variables remain available. Valid Python loops must retain Python semantics.
    """
    state = {}
    rows = []
    for code in codes:
        value, failed = None, False
        try:
            value, _ = executor.evaluate_python_code(
                code, static_tools=dict(executor.BASE_PYTHON_TOOLS), state=state, timeout_seconds=None
            )
        except executor.InterpreterError:
            failed = True
        rows.append({"value": value, "logs": str(state.get("_print_outputs", "")), "failed": failed})
    return rows
