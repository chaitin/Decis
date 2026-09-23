"""The Decis playground web service.

`server.py` is the whole program: it serves `web/` and proxies `/v1/systemone`
to the running engine with the bearer token attached. It is not part of the
`decis` package and imports nothing from it -- see `playground/Dockerfile`.
"""
