"""Yukti domain layer — pure entities, enums and state machines.

Nothing in this package performs I/O. It has no dependency on SQLAlchemy, Kafka,
Redis or the Anthropic SDK, which is what lets the FSMs and money arithmetic be
property-tested exhaustively without a running stack.
"""
