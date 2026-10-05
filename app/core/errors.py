class AppError(Exception):
    def __init__(
        self,
        code: int,
        message: str,
        error_type: str,
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.error_type = error_type
        self.status_code = status_code
