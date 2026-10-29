# KAERIS i18n — MCP server
#
# Built for stdio: the container IS the server. A client (Claude Desktop, Cursor,
# the Glama inspector) speaks JSON-RPC over stdin/stdout, so there is no port to
# expose and no daemon to supervise — run it with `docker run -i --rm`.
#
#   docker build -t kaeris-mcp .
#   docker run -i --rm kaeris-mcp                      # anonymous free tier
#   docker run -i --rm -e KAERIS_API_KEY=… kaeris-mcp  # Pro or Team/API
#   docker run -i --rm -v "$PWD:/work" -w /work kaeris-mcp   # to see your repo
#
# The four repo-aware tools (scan_repo, status, list_missing_keys,
# list_languages) read files from the working directory, so mount your project
# at /work if you want them to see it. Translation tools call the API instead.

FROM python:3.12-slim

# Deps first: this layer only rebuilds when the dependency set changes, not on
# every source edit. No compiler needed — both packages are pure Python.
# The install below is --no-deps, so THIS line decides the versions in the image.
# kaeris>=0.2.31 carries the array-aware delta and the warning split the server relies on; 0.2.39 is the CLI whose
# `check` verdict kaeris_status mirrors, so that is the floor. mcp>=1.2,<3 covers
# both SDK generations (the import falls back to 2.x's new location).
RUN pip install --no-cache-dir "kaeris>=0.2.39" "mcp>=1.2,<3"

WORKDIR /src
COPY pyproject.toml README.md ./
COPY kaeris_mcp ./kaeris_mcp
RUN pip install --no-cache-dir --no-deps . && rm -rf /src

# Unbuffered stdio is not optional here: the protocol is a conversation over
# stdout, and a buffered stream would hold replies until the buffer fills.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Nothing this server does needs root. /work is the mount point for a caller's
# repo and is owned by the runtime user so file-writing tools work when mounted.
RUN useradd --create-home --uid 10001 kaeris && mkdir -p /work && chown kaeris:kaeris /work
USER kaeris
WORKDIR /work

ENTRYPOINT ["kaeris-mcp"]
