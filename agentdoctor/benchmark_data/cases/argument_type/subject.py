def price(quantity):
    if type(quantity) is not int or quantity < 0:
        raise TypeError('quantity must be a nonnegative integer')
    return quantity * 7


def run(quantity):
    arguments = {"quantity": str(quantity)}
    return price(**arguments)


def stable(value):
    return value + 10
