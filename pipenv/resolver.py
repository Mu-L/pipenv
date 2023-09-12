import importlib.util
import json
import logging
import os
import platform
import sys
from typing import Dict

try:
    from functools import cached_property
except ImportError:
    cached_property = property


def _ensure_modules():
    spec = importlib.util.spec_from_file_location(
        "typing_extensions",
        location=os.path.join(
            os.path.dirname(__file__), "patched", "pip", "_vendor", "typing_extensions.py"
        ),
    )
    typing_extensions = importlib.util.module_from_spec(spec)
    sys.modules["typing_extensions"] = typing_extensions
    spec.loader.exec_module(typing_extensions)
    spec = importlib.util.spec_from_file_location(
        "pipenv", location=os.path.join(os.path.dirname(__file__), "__init__.py")
    )
    pipenv = importlib.util.module_from_spec(spec)
    sys.modules["pipenv"] = pipenv
    spec.loader.exec_module(pipenv)


def get_parser():
    from argparse import ArgumentParser

    parser = ArgumentParser("pipenv-resolver")
    parser.add_argument("--pre", action="store_true", default=False)
    parser.add_argument("--clear", action="store_true", default=False)
    parser.add_argument("--verbose", "-v", action="count", default=False)
    parser.add_argument(
        "--category",
        metavar="category",
        action="store",
        default=None,
    )
    parser.add_argument("--system", action="store_true", default=False)
    parser.add_argument("--parse-only", action="store_true", default=False)
    parser.add_argument(
        "--pipenv-site",
        metavar="pipenv_site_dir",
        action="store",
        default=os.environ.get("PIPENV_SITE_DIR"),
    )
    parser.add_argument(
        "--requirements-dir",
        metavar="requirements_dir",
        action="store",
        default=os.environ.get("PIPENV_REQ_DIR"),
    )
    parser.add_argument(
        "--write",
        metavar="write",
        action="store",
        default=os.environ.get("PIPENV_RESOLVER_FILE"),
    )
    parser.add_argument(
        "--constraints-file",
        metavar="constraints_file",
        action="store",
        default=None,
    )
    parser.add_argument("packages", nargs="*")
    return parser


def handle_parsed_args(parsed):
    if parsed.verbose:
        os.environ["PIPENV_VERBOSITY"] = "1"
        os.environ["PIP_RESOLVER_DEBUG"] = "1"
    if parsed.constraints_file:
        with open(parsed.constraints_file) as constraints:
            file_constraints = constraints.read().strip().split("\n")
        os.unlink(parsed.constraints_file)
        packages = {}
        for line in file_constraints:
            dep_name, pip_line = line.split(",", 1)
            packages[dep_name] = pip_line
        parsed.packages = packages
    return parsed


def _default_environment_override() -> Dict[str, str]:
    from pipenv.patched.pip._vendor.packaging.markers import format_full_version
    from pipenv.project import Project

    iver = format_full_version(sys.implementation.version)
    implementation_name = sys.implementation.name
    defaults = {
        "implementation_name": implementation_name,
        "implementation_version": iver,
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": platform.python_version(),
        "platform_python_implementation": platform.python_implementation(),
        "python_version": ".".join(platform.python_version_tuple()[:2]),
        "sys_platform": sys.platform,
    }
    project = Project()
    requires = project.parsed_pipfile.get("resolver", {})
    for k in defaults:
        if requires.get(k):
            defaults[k] = requires[k]

    return defaults


class Entry:
    """A resolved entry from a resolver run"""

    def __init__(
        self, name, entry_dict, project, resolver, reverse_deps=None, category=None
    ):
        super().__init__()
        from pipenv.utils.dependencies import (
            get_lockfile_section_using_pipfile_category,
        )
        from pipenv.utils.toml import tomlkit_value_to_python

        self.name = name
        if isinstance(entry_dict, dict):
            self.entry_dict = self.clean_initial_dict(entry_dict)
        else:
            self.entry_dict = entry_dict
        self.project = project
        self.category = category
        self.lockfile_section = get_lockfile_section_using_pipfile_category(category)
        self.pipfile = tomlkit_value_to_python(project.parsed_pipfile.get(category, {}))
        self.lockfile = project.lockfile_content.get(self.lockfile_section, {})
        self.pipfile_dict = self.pipfile.get(self.pipfile_name, {})
        if self.category != "packages" and self.name in project.lockfile_content.get(
            "default", {}
        ):
            self.lockfile_dict = project.lockfile_content["default"][name]
        else:
            self.lockfile_dict = self.lockfile.get(name, entry_dict)
        self.resolver = resolver
        self.reverse_deps = reverse_deps
        self._original_markers = None
        self._markers = None
        self._entry = None
        self._lockfile_entry = None
        self._pipfile_entry = None
        self._parent_deps = []
        self._flattened_parents = []
        self._requires = None
        self._deptree = None
        self._parents_in_pipfile = []

    @staticmethod
    def make_requirement(name=None, entry=None):
        from pipenv.utils.dependencies import from_pipfile

        return from_pipfile(name, entry)

    @classmethod
    def clean_initial_dict(cls, entry_dict):
        from pipenv.patched.pip._vendor.packaging.requirements import Requirement

        entry_dict.get("version", "")
        version = entry_dict.get("version", "")
        if isinstance(version, Requirement):
            version = str(version.specifier)
        entry_dict["version"] = cls.clean_specifier(version)
        if "name" in entry_dict:
            del entry_dict["name"]
        return entry_dict

    @classmethod
    def parse_pyparsing_exprs(cls, expr_iterable):
        from pipenv.patched.pip._vendor.pyparsing import Literal, MatchFirst

        keys = []
        expr_list = []
        expr = expr_iterable.copy()
        if isinstance(expr, Literal) or (expr.__class__.__name__ == Literal.__name__):
            keys.append(expr.match)
        elif isinstance(expr, MatchFirst) or (
            expr.__class__.__name__ == MatchFirst.__name__
        ):
            expr_list = expr.exprs
        elif isinstance(expr, list):
            expr_list = expr
        if expr_list:
            for part in expr_list:
                keys.extend(cls.parse_pyparsing_exprs(part))
        return keys

    @classmethod
    def get_markers_from_dict(cls, entry_dict):
        from pipenv.patched.pip._vendor.packaging import markers as packaging_markers
        from pipenv.utils.markers import normalize_marker_str

        marker_keys = cls.parse_pyparsing_exprs(packaging_markers.VARIABLE)
        markers = set()
        keys_in_dict = [k for k in marker_keys if k in entry_dict]
        markers = {normalize_marker_str(f"{k} {entry_dict.pop(k)}") for k in keys_in_dict}
        if "markers" in entry_dict:
            markers.add(normalize_marker_str(entry_dict["markers"]))
        if None in markers:
            markers.remove(None)
        if markers:
            entry_dict["markers"] = " and ".join(list(markers))
        else:
            markers = None
        return markers, entry_dict

    @property
    def markers(self):
        self._markers, self.entry_dict = self.get_markers_from_dict(self.entry_dict)
        return self._markers

    @markers.setter
    def markers(self, markers):
        if not markers:
            marker_str = self.marker_to_str(markers)
            if marker_str:
                self.entry.merge_markers(marker_str)
                self._markers = self.marker_to_str(self._entry.markers)
                entry_dict = self.entry_dict.copy()
                entry_dict["markers"] = self.marker_to_str(self._entry.markers)
                self.entry_dict = entry_dict

    @property
    def original_markers(self):
        original_markers, lockfile_dict = self.get_markers_from_dict(self.lockfile_dict)
        self.lockfile_dict = lockfile_dict
        self._original_markers = self.marker_to_str(original_markers)
        return self._original_markers

    @staticmethod
    def marker_to_str(marker):
        from pipenv.utils.markers import normalize_marker_str

        if not marker:
            return None
        from collections.abc import Mapping

        marker_str = None
        if isinstance(marker, Mapping):
            marker_dict, _ = Entry.get_markers_from_dict(marker)
            if marker_dict:
                marker_str = f"{marker_dict.popitem()[1]}"
        elif isinstance(marker, (list, set, tuple)):
            marker_str = " and ".join([normalize_marker_str(m) for m in marker if m])
        elif isinstance(marker, str):
            marker_str = f"{normalize_marker_str(marker)}"
        if isinstance(marker_str, str):
            return marker_str
        return None

    @cached_property
    def get_cleaned_dict(self):
        self.validate_constraints()
        if self.entry.extras != self.lockfile_entry.extras:
            entry_extras = list(self.entry.extras)
            if self.lockfile_entry.extras:
                entry_extras.extend(list(self.lockfile_entry.extras))
            self.entry_dict["extras"] = entry_extras
        if self.original_markers and not self.markers:
            original_markers = self.marker_to_str(self.original_markers)
            self.markers = original_markers
            self.entry_dict["markers"] = self.marker_to_str(original_markers)
        entry_hashes = set(self.entry_dict.get("hashes", []))
        self.entry_dict["hashes"] = sorted(entry_hashes)
        self.entry_dict["name"] = self.name
        if "version" in self.entry_dict:
            self.entry_dict["version"] = self.strip_version(self.entry_dict["version"])
        _, self.entry_dict = self.get_markers_from_dict(self.entry_dict)
        if self.resolver.index_lookup.get(self.name):
            self.entry_dict["index"] = self.resolver.index_lookup[self.name]
        return self.entry_dict

    @property
    def lockfile_entry(self):
        if self._lockfile_entry is None:
            self._lockfile_entry = self.make_requirement(self.name, self.lockfile_dict)
        return self._lockfile_entry

    @lockfile_entry.setter
    def lockfile_entry(self, entry):
        self._lockfile_entry = entry

    @property
    def pipfile_entry(self):
        if self._pipfile_entry is None:
            self._pipfile_entry = self.make_requirement(
                self.pipfile_name, self.pipfile_dict
            )
        return self._pipfile_entry

    @property
    def entry(self):
        if self._entry is None:
            self._entry = self.make_requirement(self.name, self.entry_dict)
        return self._entry

    @property
    def normalized_name(self):
        return self.entry.normalized_name

    @property
    def pipfile_name(self):
        return self.project.get_package_name_in_pipfile(self.name, category=self.category)

    @property
    def is_in_pipfile(self):
        return bool(self.pipfile_name)

    @property
    def pipfile_packages(self):
        return self.project.pipfile_package_names[self.category]

    def create_parent(self, name, specifier="*"):
        parent = self.create(
            name, specifier, self.project, self.resolver, self.reverse_deps, self.category
        )
        parent._deptree = self.deptree
        return parent

    @property
    def deptree(self):
        if not self._deptree:
            self._deptree = self.project.environment.get_package_requirements()
        return self._deptree

    @classmethod
    def create(
        cls, name, entry_dict, project, resolver, reverse_deps=None, category=None
    ):
        return cls(name, entry_dict, project, resolver, reverse_deps, category)

    @staticmethod
    def clean_specifier(specifier):
        from pipenv.patched.pip._vendor.packaging.specifiers import Specifier

        if not any(specifier.startswith(k) for k in Specifier._operators):
            if specifier.strip().lower() in ["any", "<any>", "*"]:
                return "*"
            specifier = f"=={specifier}"
        elif specifier.startswith("==") and specifier.count("=") > 3:
            specifier = f"=={specifier.lstrip('=')}"
        return specifier

    @staticmethod
    def strip_version(specifier):
        from pipenv.patched.pip._vendor.packaging.specifiers import Specifier

        op = next(iter(k for k in Specifier._operators if specifier.startswith(k)), None)
        if op:
            specifier = specifier[len(op) :]
        while op:
            op = next(
                iter(k for k in Specifier._operators if specifier.startswith(k)),
                None,
            )
            if op:
                specifier = specifier[len(op) :]
        return specifier

    @property
    def requirements(self):
        if not self._requires:
            self._requires = next(
                iter(self.project.environment.get_package_requirements(self.name)), {}
            )
        return self._requires

    @property
    def updated_version(self):
        version = str(self.entry.specifier)
        return self.strip_version(version)

    @property
    def updated_specifier(self) -> str:
        return str(self.entry.specifier)

    def get_constraints(self):
        """
        Retrieve all of the relevant constraints, aggregated from the pipfile, resolver,
        and parent dependencies and their respective conflict resolution where possible.

        :return: A set of **InstallRequirement** instances representing constraints
        :rtype: Set
        """
        return self.resolver.parsed_constraints

    def validate_constraints(self):
        """
        Retrieves the full set of available constraints and iterate over them, validating
        that they exist and that they are not causing unresolvable conflicts.

        :return: True if the constraints are satisfied by the resolution provided
        :raises: :exc:`pipenv.exceptions.DependencyConflict` if the constraints dont exist
        """
        from pipenv.exceptions import DependencyConflict
        from pipenv.patched.pip._vendor.packaging.requirements import Requirement
        from pipenv.utils import err

        constraints = self.get_constraints()
        pinned_version = self.updated_version
        for constraint in constraints:
            if not isinstance(constraint, Requirement):
                continue
            if pinned_version and not constraint.specifier.contains(
                str(pinned_version), prereleases=True
            ):
                if self.project.s.is_verbose():
                    err.print(f"Tried constraint: {constraint!r}")
                msg = (
                    f"Cannot resolve conflicting version {self.name}{constraint.specifier} "
                    f"while {self.name}{self.updated_specifier} is locked."
                )
                raise DependencyConflict(msg)
        return True

    def __getattribute__(self, key):
        result = None
        old_version = ["was_", "had_", "old_"]
        new_version = ["is_", "has_", "new_"]
        if any(key.startswith(v) for v in new_version):
            entry = Entry.__getattribute__(self, "entry")
            try:
                keystart = key.index("_") + 1
                try:
                    result = getattr(entry, key[keystart:])
                except AttributeError:
                    result = getattr(entry, key)
            except AttributeError:
                result = super().__getattribute__(key)
            return result
        if any(key.startswith(v) for v in old_version):
            lockfile_entry = Entry.__getattribute__(self, "lockfile_entry")
            try:
                keystart = key.index("_") + 1
                try:
                    result = getattr(lockfile_entry, key[keystart:])
                except AttributeError:
                    result = getattr(lockfile_entry, key)
            except AttributeError:
                result = super().__getattribute__(key)
            return result
        return super().__getattribute__(key)


def clean_results(results, resolver, project, category):
    from pipenv.utils.dependencies import (
        get_lockfile_section_using_pipfile_category,
        translate_markers,
    )

    if not project.lockfile_exists:
        return results
    lockfile = project.lockfile_content
    lockfile_section = get_lockfile_section_using_pipfile_category(category)
    reverse_deps = project.environment.reverse_dependencies()
    new_results = [
        r for r in results if r["name"] not in lockfile.get(lockfile_section, {})
    ]
    for result in results:
        name = result.get("name")
        entry_dict = result.copy()
        entry = Entry(
            name,
            entry_dict,
            project,
            resolver,
            reverse_deps=reverse_deps,
            category=category,
        )
        entry_dict = translate_markers(entry.get_cleaned_dict)
        new_results.append(entry_dict)
    return new_results


def resolve_packages(
    pre,
    clear,
    verbose,
    system,
    write,
    requirements_dir,
    packages,
    category,
    constraints=None,
):
    from pipenv.patched.pip._vendor.packaging import markers
    from pipenv.utils.internet import create_mirror_source, replace_pypi_sources
    from pipenv.utils.resolver import resolve_deps

    original_default_environment = markers.default_environment
    markers.default_environment = _default_environment_override
    pypi_mirror_source = (
        create_mirror_source(os.environ["PIPENV_PYPI_MIRROR"], "pypi_mirror")
        if "PIPENV_PYPI_MIRROR" in os.environ
        else None
    )

    if constraints:
        packages.update(constraints)

    def resolve(
        packages, pre, project, sources, clear, system, category, requirements_dir=None
    ):
        return resolve_deps(
            packages,
            project=project,
            pre=pre,
            category=category,
            sources=sources,
            clear=clear,
            allow_global=system,
            req_dir=requirements_dir,
        )

    from pipenv.project import Project

    project = Project()
    sources = (
        replace_pypi_sources(project.pipfile_sources(), pypi_mirror_source)
        if pypi_mirror_source
        else project.pipfile_sources()
    )
    results, resolver = resolve(
        packages,
        pre=pre,
        category=category,
        project=project,
        sources=sources,
        clear=clear,
        system=system,
        requirements_dir=requirements_dir,
    )
    results = clean_results(results, resolver, project, category)
    if write:
        with open(write, "w") as fh:
            if not results:
                json.dump([], fh)
            else:
                json.dump(results, fh)
    markers.default_environment = original_default_environment
    if results:
        return results
    return []


def _main(
    pre,
    clear,
    verbose,
    system,
    write,
    requirements_dir,
    packages,
    parse_only=False,
    category=None,
):
    resolve_packages(
        pre, clear, verbose, system, write, requirements_dir, packages, category
    )


def main(argv=None):
    parser = get_parser()
    parsed, remaining = parser.parse_known_args(argv)
    _ensure_modules()
    os.environ["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUNBUFFERED"] = "1"
    parsed = handle_parsed_args(parsed)
    if not parsed.verbose:
        print(parsed.verbose)
        logging.getLogger("pipenv").setLevel(logging.WARN)
    _main(
        parsed.pre,
        parsed.clear,
        parsed.verbose,
        parsed.system,
        parsed.write,
        parsed.requirements_dir,
        parsed.packages,
        parse_only=parsed.parse_only,
        category=parsed.category,
    )


if __name__ == "__main__":
    main()
