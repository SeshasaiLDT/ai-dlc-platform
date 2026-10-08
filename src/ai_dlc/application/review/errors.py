class ReviewStoreError(Exception):
    pass


class DuplicateReviewConflictError(ReviewStoreError):
    pass


class ReviewAttemptLimitError(ReviewStoreError):
    pass
