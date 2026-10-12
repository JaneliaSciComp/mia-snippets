from check_pr import check

OK_PY = "SPEC_VERSION = 1\nimport numpy as np\ndef open_array(src, io):\n    return np.zeros(1)\n"
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
    for src in ["import subprocess\n", '__import__("socket")\n', "from os import path\nimport importlib.util\n",
                "import numpy as np\nnp.load('f', allow_pickle=True)\n", "from snippets.old import snippet\n",
                "from . import x\n", "import os\nos.system('ls')\n", "import os\nos.execvp('a', [])\n"]:
        assert run(ADDED, files=with_py(OK_PY + src)), src


def test_missing_contract_fails():
    assert run(ADDED, files=with_py("def open_array(s, i): pass\n"))
    assert run(ADDED, files=with_py("SPEC_VERSION = 1\n"))


def test_tested_on_without_example_fails():
    assert run([c for c in ADDED if "examples" not in c[1]])


def test_bad_metadata_fails():
    for y in [OK_YAML.replace("new-one", "wrong"), OK_YAML.replace("version: 1", "version: 2"),
              OK_YAML.replace("[numpy]", "[numpy, scipy]")]:
        assert run(ADDED, files={**FILES, "snippets/new-one/snippet.yaml": y}), y
