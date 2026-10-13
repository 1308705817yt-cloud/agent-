from . import codec, service

def execute(quantity):
    return service.quote(**codec.build_request(quantity))
