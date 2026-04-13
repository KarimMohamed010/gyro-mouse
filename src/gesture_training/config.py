from dataclasses import dataclass


FEATURE_COLUMNS = ["ax", "ay", "az", "gx", "gy", "gz", "yaw", "pitch", "roll"]
KNOWN_LABELS = ["swipe", "shake", "circle", "wave", "idle"]


@dataclass(frozen=True)
class PipelineConfig:
    window_size: int = 100
    overlap: float = 0.5
    random_state: int = 42
    test_size: float = 0.2
    val_size: float = 0.2
    batch_size: int = 64
    epochs: int = 40

    @property
    def stride(self) -> int:
        stride = int(self.window_size * (1.0 - self.overlap))
        return max(1, stride)
