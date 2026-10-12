from check_pr import check

OK_PY = ("SPEC_VERSION = 1\nimport numpy as np\nfrom mia_harness.api import IO, Array, Source\n"
         "def open_array(src: Source, io: IO) -> Array:\n    return np.zeros(1)\n")
OK_YAML = "name: new-one\nspec_version: 1\nrequires: [numpy]\ntested_on: [rec1]\n"
FILES = {"snippets/new-one/snippet.py": OK_PY, "snippets/new-one/snippet.yaml": OK_YAML}
ADDED = [("A", p) for p in ["snippets/new-one/snippet.py", "snippets/new-one/snippet.yaml",
                            "snippets/new-one/README.md", "snippets/new-one/examples/rec1.yaml"]]


def run(changed, author="bot", files=None, existing=("old",), maint=("alice",)):
    files = FILES if files is None else files
    return check(changed, author, set(existing), set(maint), lambda p: files[p])


def with_py(src):
    return {**FILES, "snippets/new-one/snippet.py": src}


def test_good_new_snippet_passes():
    assert run(ADDED) == []


def test_bot_edits_fail():
    assert run([("M", "snippets/old/snippet.py")])
    assert run([("M", "harness/x.py")])
    assert run([("M", "WITHDRAWN.yaml")])


def test_maintainer_harness_passes_but_existing_snippet_edit_fails():
    assert run([("M", "harness/x.py")], author="alice") == []
    assert run([("M", "snippets/old/snippet.py")], author="alice")
    assert run([("D", "snippets/old/snippet.py")], author="alice")


def test_two_new_dirs_fail():
    other = {**FILES, "snippets/other/snippet.py": OK_PY}
    assert run(ADDED + [("A", "snippets/other/snippet.py")], files=other)


def test_reused_name_fails():
    assert run(ADDED, existing=("new-one",))


def test_forbidden_code_fails():
    for src in ["import subprocess\n", '__import__("socket")\n', "import importlib.util\n", "from os import system\n", "from os import *\n", "import snippets.old\n",
                "import numpy as np\nnp.load('f', allow_pickle=True)\n", "from snippets.old import snippet\n",
                "from . import x\n", "import os\nos.system('ls')\n", "import os\nos.execvp('a', [])\n"]:
        assert run(ADDED, files=with_py(OK_PY + src)), src


def test_os_path_import_passes():
    assert run(ADDED, files=with_py("from os import path\n" + OK_PY)) == []


def test_maintainer_case_insensitive_and_bad_types():
    assert run([("M", "harness/x.py")], author="Alice") == []
    for y in ["requires: [[a]]", "requires: numpy", "tested_on: rec1", "tested_on: [[x]]"]:
        assert run(ADDED, files={**FILES, "snippets/new-one/snippet.yaml": OK_YAML + y + "\n"}) , y


def test_missing_contract_fails():
    assert run(ADDED, files=with_py("def open_array(s, i): pass\n"))


def test_open_array_without_type_hints_fails():
    assert run(ADDED, files=with_py("SPEC_VERSION = 1\ndef open_array(src, io):\n    pass\n"))
    assert run(ADDED, files=with_py("SPEC_VERSION = 1\ndef open_array(src: int, io: int):\n    pass\n"))
    assert run(ADDED, files=with_py("SPEC_VERSION = 1\n"))


def test_tested_on_without_example_fails():
    assert run([c for c in ADDED if "examples" not in c[1]])


def test_bad_metadata_fails():
    for y in [OK_YAML.replace("new-one", "wrong"), OK_YAML.replace("version: 1", "version: 2"),
              OK_YAML.replace("[numpy]", "[numpy, scipy]")]:
        assert run(ADDED, files={**FILES, "snippets/new-one/snippet.yaml": y}), y


def test_only_the_harness_api_may_be_imported():
    ok = "from mia_harness.api import NeedsFetch\nimport mia_harness.api\nfrom mia_harness import api\n"
    assert run(ADDED, files=with_py(ok + OK_PY)) == []
    for src in ["from mia_harness.io import NeedsFetch\n", "import mia_harness.io\n", "from mia_harness import io\n",
                "import mia_harness\n", "from mia_harness.api import x\nfrom mia_harness import run\n"]:
        assert run(ADDED, files=with_py(src + OK_PY)), src


def test_main_reads_maintainers_and_allowlist_from_base(tmp_path, monkeypatch):
    import subprocess
    from check_pr import main

    def git(*a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=tmp_path, check=True,
                       capture_output=True)

    def write(p, s):
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text(s)

    git("init", "-q", "-b", "main")
    write("ci/maintainers.txt", "alice\n")
    write("harness/allowlist.txt", "numpy\n")
    write("snippets/old/snippet.py", OK_PY)
    git("add", "-A"); git("commit", "-qm", "base")
    git("checkout", "-qb", "pr")
    write("ci/maintainers.txt", "alice\nbob\n")         # the PR tries to make its author a maintainer
    write("harness/allowlist.txt", "numpy\nscipy\n")
    git("commit", "-qam", "pr")
    (tmp_path / "harness").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path / "harness")              # any subdirectory finds the repo root
    assert main(["--base", "main", "--author", "bob"]) == 1
    assert main(["--base", "main", "--author", "alice"]) == 0
    for p, s in {"snippet.py": OK_PY, "snippet.yaml": OK_YAML.replace("[numpy]", "[scipy]").replace("[rec1]", "[]")}.items():
        write(f"snippets/new-one/{p}", s)
    git("add", "-A"); git("commit", "-qm", "snippet")
    assert main(["--base", "main", "--author", "alice"]) == 1   # scipy is only on the PR's allowlist


def test_dynamic_code_and_interpreter_modules_fail():
    for src in ["exec('import socket')\n", "eval('1')\n", "compile('x', 'f', 'exec')\n", "import sys\n",
                "from sys import modules\n", "import builtins\n", "import runpy\n", "import code\n", "import pty\n",
                "import numpy as np\nnp.load('f', allow_pickle=1)\n", "import numpy as np\nnp.load('f', allow_pickle='y')\n",
                "import numpy as np\nnp.load('f', allow_pickle=flag)\n"]:
        assert run(ADDED, files=with_py(OK_PY + src)), src
    for src in ["import re\nre.compile('x')\n", "import numpy as np\nnp.load('f', allow_pickle=False)\n"]:
        assert run(ADDED, files=with_py(OK_PY + src)) == [], src
