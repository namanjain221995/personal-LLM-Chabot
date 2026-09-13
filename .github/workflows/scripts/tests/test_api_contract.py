"""What the public API contract gate must catch, and what it must let through.

The cases here are the ways a public surface goes wrong quietly: a document
that is still 3.0 in a 3.1 costume, a `$ref` to a schema somebody deleted, an
endpoint that appeared because a router was mounted at the wrong prefix, and
an endpoint that vanished under developers who were already calling it.
"""
from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import api_contract  # noqa: E402


REPO_EXPECTATION = (
    pathlib.Path(__file__).resolve().parent.parent / "public-api-surface.txt"
)


def _document(paths: dict | None = None, **overrides) -> dict:
    doc = {
        "openapi": "3.1.0",
        "info": {"title": "TechSara API", "version": "1.0.0"},
        "paths": paths
        if paths is not None
        else {
            "/v1/models": {
                "get": {
                    "operationId": "listModels",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    doc.update(overrides)
    return doc


@contextlib.contextmanager
def _quiet():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


class TheDocumentMustActuallyBeOpenApi31(unittest.TestCase):
    def test_a_well_formed_document_produces_no_problems(self):
        self.assertEqual(api_contract.validate_openapi_31(_document()), [])

    def test_a_30_document_is_rejected_because_generators_treat_the_versions_differently(self):
        problems = api_contract.validate_openapi_31(_document(openapi="3.0.3"))
        self.assertTrue(any("3.1.x" in p for p in problems), problems)

    def test_a_swagger_2_document_is_rejected(self):
        problems = api_contract.validate_openapi_31(_document(swagger="2.0"))
        self.assertTrue(any("Swagger 2.0" in p for p in problems), problems)

    def test_the_30_nullable_keyword_is_rejected_because_31_deleted_it(self):
        doc = _document()
        doc["components"] = {
            "schemas": {"Usage": {"type": "object", "nullable": True}}
        }
        problems = api_contract.validate_openapi_31(doc)
        self.assertTrue(any("nullable" in p for p in problems), problems)

    def test_a_ref_that_points_at_nothing_is_rejected(self):
        doc = _document()
        doc["paths"]["/v1/models"]["get"]["responses"]["200"] = {
            "description": "ok",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Gone"}}
            },
        }
        problems = api_contract.validate_openapi_31(doc)
        self.assertTrue(any("does not exist" in p for p in problems), problems)

    def test_a_ref_that_resolves_is_accepted(self):
        doc = _document()
        doc["components"] = {"schemas": {"Model": {"type": "object"}}}
        doc["paths"]["/v1/models"]["get"]["responses"]["200"] = {
            "description": "ok",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Model"}}
            },
        }
        self.assertEqual(api_contract.validate_openapi_31(doc), [])

    def test_a_ref_to_a_file_on_our_disk_is_rejected_as_unresolvable_by_a_developer(self):
        doc = _document()
        doc["paths"]["/v1/models"]["get"]["responses"]["200"] = {
            "description": "ok",
            "content": {"application/json": {"schema": {"$ref": "./schemas.json#/Model"}}},
        }
        problems = api_contract.validate_openapi_31(doc)
        self.assertTrue(any("self-contained" in p for p in problems), problems)

    def test_two_operations_sharing_an_operation_id_are_rejected(self):
        doc = _document(
            paths={
                "/v1/models": {
                    "get": {"operationId": "same", "responses": {"200": {"description": "ok"}}}
                },
                "/v1/usage": {
                    "get": {"operationId": "same", "responses": {"200": {"description": "ok"}}}
                },
            }
        )
        problems = api_contract.validate_openapi_31(doc)
        self.assertTrue(any("collide on duplicates" in p for p in problems), problems)

    def test_an_operation_with_no_responses_is_rejected(self):
        doc = _document(paths={"/v1/models": {"get": {"operationId": "listModels"}}})
        problems = api_contract.validate_openapi_31(doc)
        self.assertTrue(any("declares no `responses`" in p for p in problems), problems)

    def test_a_document_with_no_paths_at_all_is_rejected(self):
        problems = api_contract.validate_openapi_31(_document(paths={}))
        self.assertTrue(any("describes nothing" in p for p in problems), problems)

    def test_a_path_key_that_is_not_a_path_is_rejected(self):
        doc = _document(paths={"v1/models": {"get": {"responses": {"200": {"description": "ok"}}}}})
        problems = api_contract.validate_openapi_31(doc)
        self.assertTrue(any("does not start with `/`" in p for p in problems), problems)

    def test_info_without_a_title_or_version_is_rejected(self):
        problems = api_contract.validate_openapi_31(_document(info={"title": "", "version": ""}))
        self.assertTrue(any("info.title" in p for p in problems), problems)
        self.assertTrue(any("info.version" in p for p in problems), problems)


class TheSurfaceMustBeTheOneThatWasSignedOff(unittest.TestCase):
    def test_an_endpoint_nobody_reviewed_fails_the_gate(self):
        doc = _document(
            paths={
                "/v1/models": {"get": {"responses": {"200": {"description": "ok"}}}},
                "/v1/internal/debug": {"get": {"responses": {"200": {"description": "ok"}}}},
            }
        )
        problems = api_contract.compare_surface(doc, {"GET /v1/models"}, "surface.txt")
        self.assertEqual(len(problems), 1)
        self.assertIn("+ GET /v1/internal/debug", problems[0])

    def test_an_endpoint_that_disappeared_fails_the_gate(self):
        problems = api_contract.compare_surface(
            _document(), {"GET /v1/models", "GET /v1/usage"}, "surface.txt"
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("- GET /v1/usage", problems[0])

    def test_the_same_path_with_a_new_method_fails_the_gate(self):
        doc = _document(
            paths={
                "/v1/models": {
                    "get": {"responses": {"200": {"description": "ok"}}},
                    "delete": {"responses": {"204": {"description": "gone"}}},
                }
            }
        )
        problems = api_contract.compare_surface(doc, {"GET /v1/models"}, "surface.txt")
        self.assertIn("+ DELETE /v1/models", problems[0])

    def test_an_exact_match_produces_no_problems(self):
        self.assertEqual(
            api_contract.compare_surface(_document(), {"GET /v1/models"}, "surface.txt"), []
        )


class TheExpectationFileIsParsedStrictly(unittest.TestCase):
    def test_comments_and_blank_lines_are_ignored(self):
        wanted, problems = api_contract.parse_expectation(
            "# a comment\n\nGET /v1/models   # trailing\nPOST /v1/responses\n"
        )
        self.assertEqual(problems, [])
        self.assertEqual(wanted, {"GET /v1/models", "POST /v1/responses"})

    def test_a_line_that_is_not_method_and_path_is_reported(self):
        _, problems = api_contract.parse_expectation("GET\n")
        self.assertTrue(any("expected `METHOD /path`" in p for p in problems), problems)

    def test_a_verb_that_is_not_an_http_method_is_reported(self):
        _, problems = api_contract.parse_expectation("FETCH /v1/models\n")
        self.assertTrue(any("not an HTTP method" in p for p in problems), problems)

    def test_the_checked_in_expectation_file_parses_and_matches_the_contract(self):
        wanted, problems = api_contract.parse_expectation(
            REPO_EXPECTATION.read_text(encoding="utf-8")
        )
        self.assertEqual(problems, [])
        # CONTRACT-3 §7 names exactly these eight operations. If the contract
        # changes, this test and the file change together, deliberately.
        self.assertEqual(
            wanted,
            {
                "GET /v1/models",
                "GET /v1/models/{model}",
                "POST /v1/responses",
                "GET /v1/responses/{id}",
                "POST /v1/responses/{id}/cancel",
                "POST /v1/chat/completions",
                "GET /v1/usage",
                "GET /v1/openapi.json",
            },
        )


class TheJobIsLoudWhileThePackageDoesNotExist(unittest.TestCase):
    def test_a_missing_publicapi_package_passes_but_says_so_in_the_log_and_as_an_annotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _quiet() as (out, _):
                rc = api_contract.main(
                    ["--orchestrator", tmp, "--expected", str(REPO_EXPECTATION)]
                )
            printed = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("NOT ENFORCED YET", printed)
        self.assertIn("::warning title=Public API contract not enforced yet::", printed)

    def test_require_package_turns_the_skip_into_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _quiet():
                rc = api_contract.main(
                    [
                        "--orchestrator", tmp,
                        "--expected", str(REPO_EXPECTATION),
                        "--require-package",
                    ]
                )
        self.assertEqual(rc, 1)

    def test_deleting_the_expectation_file_is_a_hard_error_not_a_free_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _quiet() as (_, err):
                rc = api_contract.main(
                    ["--orchestrator", tmp, "--expected", f"{tmp}/gone.txt"]
                )
        self.assertEqual(rc, 2)
        self.assertIn("is missing", err.getvalue())


class TheDocumentIsReadFromTheRealPackage(unittest.TestCase):
    """End-to-end through the child interpreter, with a stand-in package."""

    def _tree(self, tmp: str, body: str) -> pathlib.Path:
        root = pathlib.Path(tmp)
        pkg = root / "app" / "publicapi"
        pkg.mkdir(parents=True)
        (root / "app" / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "openapi.py").write_text(body, encoding="utf-8")
        return root

    def test_a_package_exposing_public_openapi_is_read_and_validated(self):
        doc = _document()
        body = f"def public_openapi():\n    return {json.dumps(doc)}\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp, body)
            loaded, source, problems = api_contract.load_document(root)
        self.assertEqual(problems, [])
        self.assertEqual(source, "app.publicapi.openapi:public_openapi")
        self.assertEqual(loaded, doc)

    def test_a_package_with_no_documented_entry_point_is_a_failure_not_a_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp, "def something_else():\n    return {}\n")
            loaded, _, problems = api_contract.load_document(root)
        self.assertIsNone(loaded)
        self.assertTrue(any("none of the documented" in p for p in problems), problems)

    def test_a_generator_that_raises_reports_the_traceback_rather_than_passing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(
                tmp, "def public_openapi():\n    raise RuntimeError('no registry')\n"
            )
            loaded, _, problems = api_contract.load_document(root)
        self.assertIsNone(loaded)
        self.assertTrue(any("no registry" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
