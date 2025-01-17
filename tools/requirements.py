# Before calling this script:
# pip install toml pip-tools
#
# Run this script from the root of the repository
# python tools/requirements.py
#
# It will gather all project requirements and compile them into
# - a single requirements.txt file
# - a requirements-all.txt file with all dependencies
# - a requirements-dev.txt file with project and development dependencies
# - a requirements-test.txt file with project and test dependencies
#
import os
from collections import defaultdict
from typing import Any, Dict, List, Iterator, Optional
import toml


class ProjectDefinition:
    def __init__(self, definition: Dict[str, Any]) -> None:
        self.definition = definition

    @property
    def name(self) -> str:
        return self.definition["project"]["name"]

    @property
    def dependencies(self) -> List[str]:
        return self.definition["project"].get("dependencies", [])

    @property
    def optional_dependencies(self) -> Dict[str, List[str]]:
        return self.definition["project"].get("optional-dependencies", {})

    @property
    def all_dependencies(self) -> List[str]:
        return self.dependencies + [dep for deps in self.optional_dependencies.values() for dep in deps]


def all_project_definitions() -> Iterator[ProjectDefinition]:
    for root, _, files in os.walk("."):
        if "site-packages" in root or ".git" in root:
            continue
        for file in files:
            if file == "pyproject.toml":
                print(f"Found pyproject.toml in {root}")
                file_path = os.path.join(root, file)
                try:
                    with open(file_path) as f:
                        yield ProjectDefinition(toml.load(f))
                except Exception as e:
                    print(f"Failed to parse {file_path}: {e}")
                    raise


filter_out = ["resotolib", "resoto-plugin-aws"]


def filter_dependencies(deps: List[str]) -> List[str]:
    return [dep for dep in deps if not any(name in dep for name in filter_out)]


def compile_dependencies(name: Optional[str], deps: List[str]) -> None:
    delim = "-" + name if name else ""
    with open(f"requirements{delim}-in.txt", "w") as f:
        f.write("\n".join(deps))
    args = "-q --no-annotate --resolver=backtracking --upgrade --allow-unsafe --no-header  --unsafe-package n/a"
    os.system(f"pip-compile {args} --output-file requirements{delim}.txt requirements{delim}-in.txt")
    os.remove(f"requirements{delim}-in.txt")


def combine_dependencies() -> None:
    prod_dependencies = []
    optional_dependencies = defaultdict(list)
    for project in all_project_definitions():
        prod_dependencies.extend(filter_dependencies(project.dependencies))
        for name, deps in project.optional_dependencies.items():
            optional_dependencies[name].extend(filter_dependencies(deps))
    all_dependencies = prod_dependencies.copy()

    compile_dependencies(None, prod_dependencies)
    for name, deps in optional_dependencies.items():
        all_dependencies.extend(deps)
        compile_dependencies(name, deps + prod_dependencies)
    compile_dependencies("all", all_dependencies)


if __name__ == "__main__":
    combine_dependencies()
