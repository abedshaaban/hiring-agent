import os
import sys
import json
import logging
import csv
import argparse
import re
import time
from pdf import PDFHandler
from github import fetch_and_display_github_info
from models import JSONResume, EvaluationData
from typing import Any, List, Optional, Dict
from evaluator import ResumeEvaluator
from pathlib import Path
from prompt import DEFAULT_MODEL, MODEL_PARAMETERS
from transform import (
    transform_evaluation_response,
    convert_json_resume_to_text,
    convert_github_data_to_text,
    convert_blog_data_to_text,
)
from config import DEVELOPMENT_MODE

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)


def calculate_total_score(evaluation: EvaluationData) -> Dict[str, float]:
    """Calculate final evaluation score using the same rules as console output."""
    if not evaluation:
        return {"score": 0.0, "max_score": 0.0, "max_possible_score": 0.0}

    total_score = 0.0
    max_score = 0.0

    if hasattr(evaluation, "scores") and evaluation.scores:
        for category_data in evaluation.scores.model_dump().values():
            total_score += min(category_data["score"], category_data["max"])
            max_score += category_data["max"]

    if hasattr(evaluation, "bonus_points") and evaluation.bonus_points:
        total_score += evaluation.bonus_points.total

    if hasattr(evaluation, "deductions") and evaluation.deductions:
        total_score -= evaluation.deductions.total

    max_possible_score = max_score + 20
    total_score = min(total_score, max_possible_score)

    return {
        "score": round(total_score, 2),
        "max_score": round(max_score, 2),
        "max_possible_score": round(max_possible_score, 2),
    }


def print_evaluation_results(
    evaluation: EvaluationData, candidate_name: str = "Candidate"
):
    """Print evaluation results in a readable format."""
    print("\n" + "=" * 80)
    print(f"📊 RESUME EVALUATION RESULTS FOR: {candidate_name}")
    print("=" * 80)

    if not evaluation:
        print("❌ No evaluation data available")
        return

    # Calculate overall score
    total_score = 0
    max_score = 0

    if hasattr(evaluation, "scores") and evaluation.scores:
        for category_name, category_data in evaluation.scores.model_dump().items():
            category_score = min(category_data["score"], category_data["max"])
            total_score += category_score
            max_score += category_data["max"]

            # Log warning if score was capped
            if category_score < category_data["score"]:
                print(
                    f"⚠️  Warning: {category_name} score capped from {category_data['score']} to {category_score} (max: {category_data['max']})"
                )

    # Add bonus points
    if hasattr(evaluation, "bonus_points") and evaluation.bonus_points:
        total_score += evaluation.bonus_points.total

    # Subtract deductions
    if hasattr(evaluation, "deductions") and evaluation.deductions:
        total_score -= evaluation.deductions.total

    # Ensure total score doesn't exceed maximum possible score
    max_possible_score = max_score + 20  # 120 (100 categories + 20 bonus)
    if total_score > max_possible_score:
        total_score = max_possible_score
        print(f"⚠️  Warning: Total score capped at maximum possible value")

    # Overall Score
    print(f"\n🎯 OVERALL SCORE: {total_score:.1f}/{max_score}")

    # Detailed Scores
    print("\n📈 DETAILED SCORES:")
    print("-" * 60)

    if hasattr(evaluation, "scores") and evaluation.scores:
        # Define category maximums
        category_maxes = {
            "open_source": 35,
            "self_projects": 30,
            "production": 25,
            "technical_skills": 10,
        }

        # Open Source
        if hasattr(evaluation.scores, "open_source") and evaluation.scores.open_source:
            os_score = evaluation.scores.open_source
            capped_score = min(os_score.score, category_maxes["open_source"])
            print(f"🌐 Open Source:          {capped_score}/{os_score.max}")
            print(f"   Evidence: {os_score.evidence}")
            print()

        # Self Projects
        if (
            hasattr(evaluation.scores, "self_projects")
            and evaluation.scores.self_projects
        ):
            sp_score = evaluation.scores.self_projects
            capped_score = min(sp_score.score, category_maxes["self_projects"])
            print(f"🚀 Self Projects:        {capped_score}/{sp_score.max}")
            print(f"   Evidence: {sp_score.evidence}")
            print()

        # Production Experience
        if hasattr(evaluation.scores, "production") and evaluation.scores.production:
            prod_score = evaluation.scores.production
            capped_score = min(prod_score.score, category_maxes["production"])
            print(f"🏢 Production Experience: {capped_score}/{prod_score.max}")
            print(f"   Evidence: {prod_score.evidence}")
            print()

        # Technical Skills
        if (
            hasattr(evaluation.scores, "technical_skills")
            and evaluation.scores.technical_skills
        ):
            tech_score = evaluation.scores.technical_skills
            capped_score = min(tech_score.score, category_maxes["technical_skills"])
            print(f"💻 Technical Skills:     {capped_score}/{tech_score.max}")
            print(f"   Evidence: {tech_score.evidence}")
            print()

    # Bonus Points
    if hasattr(evaluation, "bonus_points") and evaluation.bonus_points:
        print(f"\n⭐ BONUS POINTS: {evaluation.bonus_points.total}")
        print("-" * 30)
        print(f"   {evaluation.bonus_points.breakdown}")

    # Deductions
    if (
        hasattr(evaluation, "deductions")
        and evaluation.deductions
        and evaluation.deductions.total > 0
    ):
        print(f"\n⚠️  DEDUCTIONS: -{evaluation.deductions.total}")
        print("-" * 30)
        if evaluation.deductions.reasons:
            print(f"   {evaluation.deductions.reasons}")

    # Key Strengths
    if hasattr(evaluation, "key_strengths") and evaluation.key_strengths:
        print(f"\n✅ KEY STRENGTHS:")
        print("-" * 30)
        for i, strength in enumerate(evaluation.key_strengths, 1):
            print(f"  {i}. {strength}")

    # Areas for Improvement
    if (
        hasattr(evaluation, "areas_for_improvement")
        and evaluation.areas_for_improvement
    ):
        print(f"\n🔧 AREAS FOR IMPROVEMENT:")
        print("-" * 30)
        for i, area in enumerate(evaluation.areas_for_improvement, 1):
            print(f"  {i}. {area}")

    print("\n" + "=" * 80)


def build_candidate_result(
    pdf_path: str,
    candidate_name: str,
    evaluation: EvaluationData,
    resume_data: JSONResume,
    github_data: dict,
) -> Dict[str, Any]:
    score_summary = calculate_total_score(evaluation)
    csv_row = transform_evaluation_response(
        file_name=os.path.basename(pdf_path),
        evaluation=evaluation,
        resume_data=resume_data,
        github_data=github_data,
    )

    return {
        "rank": None,
        "candidate_name": candidate_name,
        "source_file": os.path.abspath(pdf_path),
        "file_name": os.path.basename(pdf_path),
        **score_summary,
        "evaluation": evaluation.model_dump() if evaluation else None,
        "resume": resume_data.model_dump() if resume_data else None,
        "github": github_data or {},
        "summary": csv_row,
    }


def safe_output_stem(path: str) -> str:
    stem = Path(path).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-")
    return stem or "resume"


def write_candidate_json(result: Dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{safe_output_stem(result['file_name'])}.json"
    logger.info("Writing candidate result JSON to %s", output_path)
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return output_path


def write_ranked_outputs(results: List[Dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    ranked_json_path = output_dir / "ranked_results.json"
    logger.info("Writing ranked JSON results to %s", ranked_json_path)
    ranked_json_path.write_text(
        json.dumps(results, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    csv_path = output_dir / "ranked_results.csv"
    logger.info("Writing ranked CSV results to %s", csv_path)
    fieldnames = [
        "rank",
        "candidate_name",
        "score",
        "max_score",
        "max_possible_score",
        "file_name",
        "source_file",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow({field: result.get(field) for field in fieldnames})

    markdown_path = output_dir / "ranked_results.md"
    logger.info("Writing ranked Markdown results to %s", markdown_path)
    lines = [
        "# Ranked Resume Results",
        "",
        "| Rank | Candidate | Score | File |",
        "| ---: | --- | ---: | --- |",
    ]
    for result in results:
        lines.append(
            "| {rank} | {candidate} | {score}/{max_possible} | {file_name} |".format(
                rank=result.get("rank", ""),
                candidate=result.get("candidate_name", ""),
                score=result.get("score", ""),
                max_possible=result.get("max_possible_score", ""),
                file_name=result.get("file_name", ""),
            )
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def discover_pdf_paths(input_path: str) -> List[str]:
    path = Path(input_path).expanduser()
    logger.info("Discovering PDF input from %s", path)

    if path.is_dir():
        pdf_paths = [str(pdf) for pdf in sorted(path.rglob("*.pdf"))]
        logger.info("Found %s PDF file(s) under %s", len(pdf_paths), path)
        return pdf_paths

    if path.is_file() and path.suffix.lower() == ".pdf":
        logger.info("Using single PDF file %s", path)
        return [str(path)]

    if not path.is_file():
        raise FileNotFoundError(f"Input path '{input_path}' does not exist.")

    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            raw_paths = payload
        elif isinstance(payload, dict):
            raw_paths = payload.get("files") or payload.get("pdfs") or []
        else:
            raw_paths = []
    elif path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            raw_paths = []
            for row in reader:
                raw_paths.append(
                    row.get("path")
                    or row.get("pdf_path")
                    or row.get("file")
                    or row.get("file_path")
                    or ""
                )
    else:
        raw_paths = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    base_dir = path.parent
    pdf_paths = []
    for raw_path in raw_paths:
        candidate = Path(str(raw_path)).expanduser()
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        if candidate.suffix.lower() != ".pdf":
            logger.warning("Skipping non-PDF input: %s", candidate)
            continue
        pdf_paths.append(str(candidate))

    logger.info("Resolved %s PDF file(s) from manifest %s", len(pdf_paths), path)
    return pdf_paths


def _evaluate_resume(
    resume_data: JSONResume, github_data: dict = None, blog_data: dict = None
) -> Optional[EvaluationData]:
    """Evaluate the resume using AI and display results."""

    logger.info("Preparing final resume evaluation with model %s", DEFAULT_MODEL)
    model_params = MODEL_PARAMETERS.get(DEFAULT_MODEL)
    evaluator = ResumeEvaluator(model_name=DEFAULT_MODEL, model_params=model_params)

    # Convert JSON resume data to text
    resume_text = convert_json_resume_to_text(resume_data)
    logger.info("Converted extracted resume data to %s characters", len(resume_text))

    # Add GitHub data if available
    if github_data:
        github_text = convert_github_data_to_text(github_data)
        resume_text += github_text
        logger.info(
            "Added GitHub enrichment data for evaluation (%s characters)",
            len(github_text),
        )
    else:
        logger.info("No GitHub enrichment data available for evaluation")

    # Add blog data if available
    if blog_data:
        blog_text = convert_blog_data_to_text(blog_data)
        resume_text += blog_text
        logger.info(
            "Added blog enrichment data for evaluation (%s characters)",
            len(blog_text),
        )

    # Evaluate the enhanced resume
    logger.info("Sending final evaluation request to the LLM")
    evaluation_result = evaluator.evaluate_resume(resume_text)
    logger.info("Final evaluation completed")

    # print(evaluation_result)

    return evaluation_result


def is_valid_resume_data(resume_data: JSONResume) -> bool:
    """Check if the resume data has at least some extracted core content."""
    if not resume_data:
        return False
    core_sections = [
        resume_data.basics,
        resume_data.work,
        resume_data.education,
        resume_data.skills,
        resume_data.projects,
    ]
    return any(section is not None for section in core_sections)


def find_profile(profiles, network):
    if not profiles:
        return None
    return next(
        (p for p in profiles if p.network and p.network.lower() == network.lower()),
        None,
    )


def main(pdf_path, output_dir: Optional[str] = None, print_results: bool = True):
    pipeline_start = time.monotonic()
    logger.info("Starting scoring pipeline for %s", pdf_path)

    # Create cache filename based on PDF path
    cache_filename = (
        f"cache/resumecache_{os.path.basename(pdf_path).replace('.pdf', '')}.json"
    )
    github_cache_filename = (
        f"cache/githubcache_{os.path.basename(pdf_path).replace('.pdf', '')}.json"
    )

    resume_data = None
    cache_loaded = False

    # Check if cache exists and we're in development mode
    if DEVELOPMENT_MODE and os.path.exists(cache_filename):
        logger.info("Loading cached resume extraction from %s", cache_filename)
        try:
            cached_data = json.loads(Path(cache_filename).read_text(encoding="utf-8"))
            loaded_resume = JSONResume(**cached_data)
            if not is_valid_resume_data(loaded_resume):
                raise ValueError("Cached resume data contains no core content")
            resume_data = loaded_resume
            cache_loaded = True
            logger.info("Resume extraction cache is valid; skipping PDF parsing")
        except Exception as e:
            logger.warning("Invalid cache file %s: %s", cache_filename, e)
            logger.info("Ignoring cache and reprocessing PDF")
            try:
                os.remove(cache_filename)
            except Exception as delete_err:
                logger.warning(
                    "Failed to delete invalid cache file %s: %s",
                    cache_filename,
                    delete_err,
                )

    if not cache_loaded:
        logger.info(
            "Extracting structured resume data from PDF%s",
            f" and will cache to {cache_filename}" if DEVELOPMENT_MODE else "",
        )
        pdf_handler = PDFHandler()
        resume_data = pdf_handler.extract_json_from_pdf(pdf_path)

        if resume_data == None:
            logger.error("Resume extraction failed for %s", pdf_path)
            return None

        if DEVELOPMENT_MODE:
            if is_valid_resume_data(resume_data):
                os.makedirs(os.path.dirname(cache_filename), exist_ok=True)
                logger.info("Writing resume extraction cache to %s", cache_filename)
                Path(cache_filename).write_text(
                    json.dumps(resume_data.model_dump(), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            else:
                logger.warning(
                    "Newly extracted resume data is empty/invalid. Skipping cache write."
                )

    # Check if cache exists and we're in development mode
    github_data = {}
    github_cache_loaded = False
    if DEVELOPMENT_MODE and os.path.exists(github_cache_filename):
        logger.info("Loading cached GitHub enrichment from %s", github_cache_filename)
        try:
            loaded_github = json.loads(
                Path(github_cache_filename).read_text(encoding="utf-8")
            )
            if (
                not isinstance(loaded_github, dict)
                or not loaded_github
                or "profile" not in loaded_github
            ):
                raise ValueError("Cached GitHub data is invalid or empty")
            github_data = loaded_github
            github_cache_loaded = True
            logger.info("GitHub enrichment cache is valid; skipping API fetch")
        except Exception as e:
            logger.warning("Invalid GitHub cache file %s: %s", github_cache_filename, e)
            logger.info("Ignoring GitHub cache and refetching")
            try:
                os.remove(github_cache_filename)
            except Exception as delete_err:
                logger.warning(
                    "Failed to delete invalid GitHub cache file %s: %s",
                    github_cache_filename,
                    delete_err,
                )

    if not github_cache_loaded:
        # Add validation to handle None values
        profiles = []
        if resume_data and hasattr(resume_data, "basics") and resume_data.basics:
            profiles = resume_data.basics.profiles or []
        github_profile = find_profile(profiles, "Github")

        if github_profile:
            logger.info(
                "Fetching GitHub enrichment from %s%s",
                github_profile.url,
                (
                    f" and will cache to {github_cache_filename}"
                    if DEVELOPMENT_MODE
                    else ""
                ),
            )
            github_data = fetch_and_display_github_info(github_profile.url)

            if (
                DEVELOPMENT_MODE
                and github_data
                and isinstance(github_data, dict)
                and "profile" in github_data
            ):
                os.makedirs(os.path.dirname(github_cache_filename), exist_ok=True)
                logger.info(
                    "Writing GitHub enrichment cache to %s", github_cache_filename
                )
                Path(github_cache_filename).write_text(
                    json.dumps(github_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        else:
            logger.info("No GitHub profile found in extracted resume data")

    score = _evaluate_resume(resume_data, github_data)

    # Get candidate name for display
    candidate_name = os.path.basename(pdf_path).replace(".pdf", "")
    if (
        resume_data
        and hasattr(resume_data, "basics")
        and resume_data.basics
        and resume_data.basics.name
    ):
        candidate_name = resume_data.basics.name

    if print_results:
        print_evaluation_results(score, candidate_name)

    result = build_candidate_result(
        pdf_path=pdf_path,
        candidate_name=candidate_name,
        evaluation=score,
        resume_data=resume_data,
        github_data=github_data,
    )

    if output_dir:
        write_candidate_json(result, Path(output_dir))

    if DEVELOPMENT_MODE:
        csv_row = result["summary"]

        # Write CSV row to file
        csv_path = "resume_evaluations.csv"
        file_exists = os.path.exists(csv_path)
        logger.info("Appending development CSV row to %s", csv_path)

        with open(csv_path, "a", newline="", encoding="utf-8") as csvfile:
            fieldnames = list(csv_row.keys())
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

            # Write headers if file doesn't exist
            if not file_exists:
                writer.writeheader()

            # Write the row
            writer.writerow(csv_row)

    elapsed = time.monotonic() - pipeline_start
    logger.info(
        "Completed scoring pipeline for %s in %.1fs",
        candidate_name,
        elapsed,
    )
    return result


def rank_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked_results = sorted(
        results,
        key=lambda result: (
            -result.get("score", 0),
            result.get("candidate_name", "").lower(),
        ),
    )
    for index, result in enumerate(ranked_results, 1):
        result["rank"] = index
    return ranked_results


def run_batch(input_path: str, output_dir: str, print_results: bool = True) -> int:
    pdf_paths = discover_pdf_paths(input_path)

    if not pdf_paths:
        print(f"No PDF files found in '{input_path}'.")
        return 1

    output_path = Path(output_dir)
    results = []
    failures = []

    for index, pdf_path in enumerate(pdf_paths, 1):
        candidate_start = time.monotonic()
        logger.info("[%s/%s] Scoring %s", index, len(pdf_paths), pdf_path)
        try:
            result = main(pdf_path, output_dir=output_dir, print_results=print_results)
            if result:
                results.append(result)
            else:
                failures.append(
                    {
                        "source_file": os.path.abspath(pdf_path),
                        "error": "No result returned from scoring pipeline.",
                    }
                )
        except Exception as exc:
            logger.exception("Failed to score %s", pdf_path)
            failures.append(
                {"source_file": os.path.abspath(pdf_path), "error": str(exc)}
            )
        finally:
            candidate_elapsed = time.monotonic() - candidate_start
            logger.info(
                "[%s/%s] Finished %s in %.1fs",
                index,
                len(pdf_paths),
                pdf_path,
                candidate_elapsed,
            )

    ranked_results = rank_results(results)
    for result in ranked_results:
        write_candidate_json(result, output_path)

    write_ranked_outputs(ranked_results, output_path)

    if failures:
        logger.info("Writing failure report to %s", output_path / "failures.json")
        (output_path / "failures.json").write_text(
            json.dumps(failures, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print(f"\nWrote {len(ranked_results)} ranked result(s) to {output_path}")
    if failures:
        print(f"{len(failures)} resume(s) failed. See {output_path / 'failures.json'}")

    return 0 if ranked_results else 1


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Score one resume PDF or batch-score PDFs into ranked output files."
    )
    parser.add_argument(
        "pdf_path",
        nargs="?",
        help="Backward-compatible single PDF path.",
    )
    parser.add_argument(
        "-i",
        "--input",
        dest="input_path",
        help="PDF, directory of PDFs, or manifest file (.txt, .csv, .json).",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        help="Directory where per-candidate JSON and ranked summary files are written.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Skip the detailed console report for each candidate.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    input_path = args.input_path or args.pdf_path

    if not input_path:
        print("Usage: python score.py <pdf_path>")
        print(
            "   or: python score.py --input <pdf|directory|manifest> --output-dir <dir>"
        )
        exit(1)

    if args.output_dir:
        exit(run_batch(input_path, args.output_dir, print_results=not args.quiet))

    if not os.path.exists(input_path):
        print(f"Error: File '{input_path}' does not exist.")
        exit(1)

    if Path(input_path).suffix.lower() != ".pdf":
        print("Error: batch input requires --output-dir.")
        exit(1)

    main(input_path, print_results=not args.quiet)
