"""Approval status shared by the approval service and the Agent Harness SDK."""

from enum import StrEnum


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
