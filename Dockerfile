# syntax=docker/dockerfile:1.4

FROM archlinux:base-devel AS libriichi_build

RUN pacman-key --init \
    && pacman-key --populate archlinux \
    && pacman -Syu --noconfirm --needed rust python python-pip \
    && pacman -Scc --noconfirm

WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY libriichi ./libriichi
COPY exe-wrapper ./exe-wrapper
RUN cargo build -p libriichi --lib --release

FROM archlinux:base

RUN pacman-key --init \
    && pacman-key --populate archlinux \
    && pacman -Syu --noconfirm --needed python python-pip \
    && pip install --break-system-packages --no-cache-dir torch numpy toml tqdm tensorboard \
    && pacman -Scc --noconfirm

WORKDIR /mortal

COPY mortal ./
COPY --from=libriichi_build /app/target/release/libriichi.so ./libriichi.so

RUN cp config.example.toml config.toml \
    && python - <<'PY'
from pathlib import Path

cfg = Path("config.toml")
text = cfg.read_text(encoding="utf-8")
text = text.replace("state_file = '/path/to/mortal.pth'", "state_file = '/mnt/mj_model/mortal.pth'", 1)
text = text.replace("best_state_file = '/path/to/best.pth'", "best_state_file = '/mnt/mj_model/best.pth'", 1)
text = text.replace("tensorboard_dir = '/path/to/dir'", "tensorboard_dir = '/tmp/mortal/tensorboard'", 1)
text = text.replace("device = 'cuda:0'", "device = 'cpu'", 1)
text = text.replace("state_file = '/path/to/grp.pth'", "state_file = '/mnt/mj_model/grp.pth'", 1)
cfg.write_text(text, encoding="utf-8")
PY

ENV MORTAL_CFG=/mortal/config.toml
VOLUME ["/mnt"]

ENTRYPOINT ["python", "mortal.py"]
CMD []
