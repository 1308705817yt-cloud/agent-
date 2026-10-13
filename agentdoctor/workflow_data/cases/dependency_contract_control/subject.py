from workflow import dispatch
def run(quantity):
    return dispatch.execute(quantity)


def stable(value):
    return value + 10
