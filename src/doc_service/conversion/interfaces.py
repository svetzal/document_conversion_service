from dataclasses import dataclass
from typing import Protocol


class ConverterGateway(Protocol):
    def convert_to_markdown(self, input_uri: str) -> str:
        """Convert the given input file into Markdown synchronously.
        This is a blocking call; callers should offload to threads if needed.
        """


class StorageGateway(Protocol):
    def job_dir(self, job_id: str) -> str:
        ...

    def save_job(self, job: dict[str, object]) -> None:
        ...

    def load_job(self, job_id: str) -> dict[str, object]:
        ...


class SecurityGateway(Protocol):
    def new_token(self) -> str:
        ...

    def hash_token(self, token: str) -> str:
        ...

    def verify(self, phc_hash: str, token: str) -> bool:
        ...


@dataclass(frozen=True)
class JobPaths:
    job_dir: str
    input_dir: str
    output_dir: str
    artifacts_dir: str
    input_path: str
