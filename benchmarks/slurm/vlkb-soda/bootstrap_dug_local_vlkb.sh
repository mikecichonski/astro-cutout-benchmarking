#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$HOME/vlkb-soda}"
AST_PREFIX="${AST_PREFIX:-$HOME/opt/ast-9.2.9}"
CFITSIO_PREFIX="${CFITSIO_PREFIX:-$HOME/opt/cfitsio}"
AST_SRC_PARENT="${AST_SRC_PARENT:-$HOME/opt/src}"
AST_TARBALL="$ROOT/docker/deps/ast-9.2.9.tar.gz"
CFITSIO_URL="${CFITSIO_URL:-https://heasarc.gsfc.nasa.gov/FTP/software/fitsio/c/cfitsio_latest.tar.gz}"
ENV_SCRIPT="${ENV_SCRIPT:-$HOME/vlkb-dug-env.sh}"

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

find_dir_with_file() {
  local filename="$1"
  shift
  local dir
  for dir in "$@"; do
    [ -n "$dir" ] || continue
    if [ -f "$dir/$filename" ]; then
      printf '%s\n' "$dir"
      return 0
    fi
  done
  return 1
}

find_first_match_dir() {
  local pattern="$1"
  shift
  local hit
  hit="$(find "$@" -path "$pattern" -print -quit 2>/dev/null || true)"
  [ -n "$hit" ] || return 1
  dirname "$hit"
}

ensure_module_cmd() {
  if ! have_cmd module && [ -f /etc/profile.d/modules.sh ]; then
    # shellcheck disable=SC1091
    source /etc/profile.d/modules.sh
  fi
}

ensure_ast() {
  if [ -f "$AST_PREFIX/include/ast.h" ]; then
    echo "AST already installed at $AST_PREFIX"
    return 0
  fi

  if [ ! -f "$AST_TARBALL" ]; then
    echo "Missing AST tarball: $AST_TARBALL" >&2
    exit 1
  fi

  mkdir -p "$AST_SRC_PARENT"
  if [ ! -d "$AST_SRC_PARENT/ast-9.2.9" ]; then
    echo "Extracting AST source"
    tar -xf "$AST_TARBALL" -C "$AST_SRC_PARENT"
  fi

  echo "Building AST into $AST_PREFIX"
  cd "$AST_SRC_PARENT/ast-9.2.9"
  ./configure --prefix="$AST_PREFIX"
  make -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 8)"
  make install
}

discover_cfitsio_include() {
  find_dir_with_file \
    fitsio.h \
    "$CFITSIO_PREFIX/include" \
    "${CFITSIO_ROOT:-}/include" \
    "${EBROOTCFITSIO:-}/include" \
    /usr/include/cfitsio \
    /usr/include \
    /usr/local/include \
    /opt/include \
    /apps/include \
    /software/include \
    || find_first_match_dir '*/fitsio.h' /usr /opt /apps /software "$HOME"
}

discover_cfitsio_lib() {
  find_dir_with_file \
    libcfitsio.so \
    "$CFITSIO_PREFIX/lib" \
    "$CFITSIO_PREFIX/lib64" \
    "${CFITSIO_ROOT:-}/lib" \
    "${CFITSIO_ROOT:-}/lib64" \
    "${EBROOTCFITSIO:-}/lib" \
    "${EBROOTCFITSIO:-}/lib64" \
    /usr/lib64 \
    /usr/lib \
    /usr/local/lib \
    /opt/lib64 \
    /opt/lib \
    /apps/lib64 \
    /apps/lib \
    /software/lib64 \
    /software/lib \
    || find_first_match_dir '*/libcfitsio.so*' /usr /opt /apps /software "$HOME"
}

download_file() {
  local url="$1"
  local out="$2"

  if have_cmd curl; then
    curl -L "$url" -o "$out"
  elif have_cmd wget; then
    wget -O "$out" "$url"
  else
    echo "Need curl or wget to download $url" >&2
    exit 1
  fi
}

ensure_cfitsio() {
  local tarball="$AST_SRC_PARENT/cfitsio_latest.tar.gz"
  local src_dir

  if [ -f "$CFITSIO_PREFIX/include/fitsio.h" ] && { [ -e "$CFITSIO_PREFIX/lib/libcfitsio.so" ] || [ -e "$CFITSIO_PREFIX/lib64/libcfitsio.so" ] || [ -e "$CFITSIO_PREFIX/lib/libcfitsio.a" ]; }; then
    echo "CFITSIO already installed at $CFITSIO_PREFIX"
    return 0
  fi

  mkdir -p "$AST_SRC_PARENT"
  echo "Downloading CFITSIO source"
  download_file "$CFITSIO_URL" "$tarball"

  src_dir="$AST_SRC_PARENT/$(tar -tf "$tarball" | cut -d/ -f1 | sed '/^$/d' | sort -u | head -1)"
  rm -rf "$src_dir"
  echo "Extracting CFITSIO source"
  tar -xf "$tarball" -C "$AST_SRC_PARENT"

  echo "Building CFITSIO into $CFITSIO_PREFIX"
  cd "$src_dir"
  ./configure --prefix="$CFITSIO_PREFIX"
  make -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 8)"
  make install
}

write_env_script() {
  local ast_lib_dir="$1"
  local cfitsio_inc_dir="$2"
  local cfitsio_lib_dir="$3"

  cat > "$ENV_SCRIPT" <<EOF
#!/usr/bin/env bash
set -euo pipefail

if ! command -v module >/dev/null 2>&1 && [ -f /etc/profile.d/modules.sh ]; then
  source /etc/profile.d/modules.sh
fi

module load gcc/9.2.0
module load python/3.10.13

export AST_PREFIX="$AST_PREFIX"
export CFITSIO_PREFIX="$CFITSIO_PREFIX"
export CPATH="$AST_PREFIX/include:$cfitsio_inc_dir\${CPATH:+:\$CPATH}"
export CPLUS_INCLUDE_PATH="$AST_PREFIX/include:$cfitsio_inc_dir\${CPLUS_INCLUDE_PATH:+:\$CPLUS_INCLUDE_PATH}"
export LIBRARY_PATH="$ast_lib_dir:$cfitsio_lib_dir\${LIBRARY_PATH:+:\$LIBRARY_PATH}"
export LD_LIBRARY_PATH="$ast_lib_dir:$cfitsio_lib_dir\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
export PATH="$AST_PREFIX/bin:$CFITSIO_PREFIX/bin:\${PATH}"
EOF
  chmod +x "$ENV_SCRIPT"
}

repair_vlkb_makefile() {
  local makefile="$ROOT/data-access/engine/src/vlkb/Makefile"

  if [ ! -f "$makefile" ]; then
    echo "Missing $makefile" >&2
    exit 1
  fi

  echo "Repairing vlkb Makefile for local-only build"
  sed -i 's|^DAVIX_SRC := $(SRC_DIR)/imcopydav.cpp$|DAVIX_SRC = $(SRC_DIR)/imcopydav.cpp|' "$makefile"
  sed -i 's|^CFLAGS_COMMON   = -c -Wstrict-prototypes $(FLAGS_COMMON) -DVLKB_WITH_DAVIX=$(VLKB_WITH_DAVIX)$|CFLAGS_COMMON   = -c -Wstrict-prototypes $(FLAGS_COMMON)|' "$makefile"
  sed -i 's|^CXX_DEFAULT_FLAGS = -c -std=c++11 $(FLAGS_COMMON)$|CXX_DEFAULT_FLAGS = -c -std=c++11 $(FLAGS_COMMON) -DVLKB_WITH_DAVIX=$(VLKB_WITH_DAVIX)|' "$makefile"
}

verify_local_only_patch() {
  grep -q 'NO_DAVIX' "$ROOT/data-access/engine/src/vlkb/Makefile" || {
    echo "Missing NO_DAVIX support in $ROOT/data-access/engine/src/vlkb/Makefile" >&2
    exit 1
  }

  grep -q 'VLKB_WITH_DAVIX' "$ROOT/data-access/engine/src/vlkb/src/main.cpp" || {
    echo "Missing VLKB_WITH_DAVIX support in $ROOT/data-access/engine/src/vlkb/src/main.cpp" >&2
    exit 1
  }
}

main() {
  local cfitsio_inc_dir
  local cfitsio_lib_dir
  local ast_lib_dir
  local binary

  ensure_module_cmd
  echo "Loading gcc/9.2.0"
  module load gcc/9.2.0
  echo "Loading python/3.10.13"
  module load python/3.10.13

  ensure_ast
  ensure_cfitsio

  ast_lib_dir="$AST_PREFIX/lib"
  if [ ! -e "$ast_lib_dir/libast.so" ] && [ ! -e "$ast_lib_dir/libast.a" ]; then
    ast_lib_dir="$AST_PREFIX/lib64"
  fi

  cfitsio_inc_dir="$(discover_cfitsio_include || true)"
  cfitsio_lib_dir="$(discover_cfitsio_lib || true)"

  if [ -z "$cfitsio_inc_dir" ] || [ ! -f "$cfitsio_inc_dir/fitsio.h" ]; then
    echo "Could not locate fitsio.h after installing CFITSIO." >&2
    exit 1
  fi

  if [ -z "$cfitsio_lib_dir" ]; then
    echo "Could not locate libcfitsio after installing CFITSIO." >&2
    exit 1
  fi

  write_env_script "$ast_lib_dir" "$cfitsio_inc_dir" "$cfitsio_lib_dir"

  # shellcheck disable=SC1090
  echo "Sourcing $ENV_SCRIPT"
  source "$ENV_SCRIPT"

  echo "Verifying headers"
  echo '#include <ast.h>' | g++ -E -x c++ - >/dev/null
  echo '#include <fitsio.h>' | g++ -E -x c++ - >/dev/null

  verify_local_only_patch
  repair_vlkb_makefile

  cd "$ROOT"
  echo "Building vlkb common library"
  make -C data-access/engine/src/common clean
  make -C data-access/engine/src/vlkb clean
  make -C data-access/engine/src/common
  echo "Building local-only vlkb binary"
  make -C data-access/engine/src/vlkb NO_DAVIX=1 NO_CSV=1

  binary="$ROOT/data-access/engine/src/vlkb/bin/vlkb"
  echo "Verifying vlkb binary"
  "$binary" --help >/dev/null

  cat <<EOF

Build complete.
Binary: $binary
Env:    $ENV_SCRIPT
EOF
}

main "$@"
