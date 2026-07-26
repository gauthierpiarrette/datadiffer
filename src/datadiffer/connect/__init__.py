"""Source connectors. Each exposes a small protocol the engine consumes:
read a schema, stream Arrow, report row/byte estimates, and (where the
source supports it) list declared keys."""
