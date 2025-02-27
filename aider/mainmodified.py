#Additional removal
import configparser
import json
import os
import re
import sys
import threading
import traceback
from dataclasses import fields
from pathlib import Path

try:
    import git
except ImportError:
    git = None

import importlib_resources
from dotenv import load_dotenv
from aider import __version__, models, urls, utils
from aider.coders import Coder
from aider.coders.base_coder import UnknownEditFormat
from aider.commands import Commands
from aider.format_settings import format_settings, scrub_sensitive_info
from aider.llm import litellm  # noqa: F401; properly init litellm on launch
from aider.models import ModelSettings
from aider.repo import ANY_GIT_ERROR, GitRepo


def check_config_files_for_yes(config_files):
    found = False
    for config_file in config_files:
        if Path(config_file).exists():
            try:
                with open(config_file, "r") as f:
                    for line in f:
                        if line.strip().startswith("yes:"):
                            print("Configuration error detected.")
                            print(f"The file {config_file} contains a line starting with 'yes:'")
                            print("Please replace 'yes:' with 'yes-always:' in this file.")
                            found = True
            except Exception:
                pass
    return found


def get_git_root():
    """Try and guess the git repo, since the conf.yml can be at the repo root"""
    try:
        repo = git.Repo(search_parent_directories=True)
        return repo.working_tree_dir
    except (git.InvalidGitRepositoryError, FileNotFoundError):
        return None


def guessed_wrong_repo(git_root, fnames, git_dname):
    """After we parse the args, we can determine the real repo. Did we guess wrong?"""

    try:
        check_repo = Path(GitRepo(fnames, git_dname).root).resolve()
    except (OSError,) + ANY_GIT_ERROR:
        return

    # we had no guess, rely on the "true" repo result
    if not git_root:
        return str(check_repo)

    git_root = Path(git_root).resolve()
    if check_repo == git_root:
        return

    return str(check_repo)


def make_new_repo(git_root):
    try:
        repo = git.Repo.init(git_root)
        check_gitignore(git_root, False)
    except ANY_GIT_ERROR as err:  # issue #1233
        print(f"Unable to create git repo in {git_root}")
        print(str(err))
        return

    print(f"Git repository created in {git_root}")
    return repo

def setup_git(git_root):
    if git is None:
        return

    try:
        cwd = Path.cwd()
    except OSError:
        cwd = None

    repo = None

    if git_root:
        try:
            repo = git.Repo(git_root)
        except ANY_GIT_ERROR:
            pass
    elif cwd == Path.home():
        print(
            "You should probably run aider in your project's directory, not your home dir."
        )
        return
    elif cwd and input(
        "No git repo found, create one to track aider's changes (recommended)?"
    ).lower() in ("y", "yes"):
        git_root = str(cwd.resolve())
        repo = make_new_repo(git_root)

    if not repo:
        return

    user_name = None
    user_email = None
    with repo.config_reader() as config:
        try:
            user_name = config.get_value("user", "name", None)
        except (configparser.NoSectionError, configparser.NoOptionError):
            pass
        try:
            user_email = config.get_value("user", "email", None)
        except (configparser.NoSectionError, configparser.NoOptionError):
            pass

    if user_name and user_email:
        return repo.working_tree_dir

    with repo.config_writer() as git_config:
        if not user_name:
            git_config.set_value("user", "name", "Your Name")
            print('Update git name with: git config user.name "Your Name"')
        if not user_email:
            git_config.set_value("user", "email", "you@example.com")
            print('Update git email with: git config user.email "you@example.com"')

    return repo.working_tree_dir


def check_gitignore(git_root, ask=True):
    if not git_root:
        return

    try:
        repo = git.Repo(git_root)
        if repo.ignored(".aider") and repo.ignored(".env"):
            return
    except ANY_GIT_ERROR:
        pass

    patterns = [".aider*", ".env"]
    patterns_to_add = []

    gitignore_file = Path(git_root) / ".gitignore"
    if gitignore_file.exists():
        try:
            with open(gitignore_file, "r") as f:
                content = f.read()
            existing_lines = content.splitlines()
            for pat in patterns:
                if pat not in existing_lines:
                    if "*" in pat or (Path(git_root) / pat).exists():
                        patterns_to_add.append(pat)
        except OSError as e:
            print(f"Error when trying to read {gitignore_file}: {e}")
            return
    else:
        content = ""
        patterns_to_add = patterns

    if not patterns_to_add:
        return

    if ask and input(f"Add {', '.join(patterns_to_add)} to .gitignore (recommended)?").lower() not in (
        "y",
        "yes",
    ):
        return

    if content and not content.endswith("\n"):
        content += "\n"
    content += "\n".join(patterns_to_add) + "\n"

    try:
        with open(gitignore_file, "w") as f:
            f.write(content)
        print(f"Added {', '.join(patterns_to_add)} to .gitignore")
    except OSError as e:
        print(f"Error when trying to write to {gitignore_file}: {e}")
        print(
            "Try running with appropriate permissions or manually add these patterns to .gitignore:"
        )
        for pattern in patterns_to_add:
            print(f"  {pattern}")



def generate_search_path_list(default_file, git_root, command_line_file):
    files = []
    files.append(Path.home() / default_file)  # homedir
    if git_root:
        files.append(Path(git_root) / default_file)  # git root
    files.append(default_file)
    if command_line_file:
        files.append(command_line_file)

    resolved_files = []
    for fn in files:
        try:
            resolved_files.append(Path(fn).resolve())
        except OSError:
            pass

    files = resolved_files
    files.reverse()
    uniq = []
    for fn in files:
        if fn not in uniq:
            uniq.append(fn)
    uniq.reverse()
    files = uniq
    files = list(map(str, files))
    files = list(dict.fromkeys(files))

    return files


def register_models(git_root, model_settings_fname, verbose=False):
    model_settings_files = generate_search_path_list(
        ".aider.model.settings.yml", git_root, model_settings_fname
    )

    try:
        files_loaded = models.register_models(model_settings_files)
        if len(files_loaded) > 0:
            if verbose:
                print("Loaded model settings from:")
            for file_loaded in files_loaded:
                print(f"  - {file_loaded}")  # noqa: E221
        elif verbose:
            print("No model settings files loaded")
    except Exception as e:
        print(f"Error loading aider model settings: {e}")
        return 1

    if verbose:
        print("Searched for model settings files:")
        for file in model_settings_files:
            print(f"  - {file}")

    return None


def load_dotenv_files(git_root, dotenv_fname, encoding="utf-8"):
    dotenv_files = generate_search_path_list(
        ".env",
        git_root,
        dotenv_fname,
    )
    loaded = []
    for fname in dotenv_files:
        try:
            if Path(fname).exists():
                load_dotenv(fname, override=True, encoding=encoding)
                loaded.append(fname)
        except OSError as e:
            print(f"OSError loading {fname}: {e}")
        except Exception as e:
            print(f"Error loading {fname}: {e}")
    return loaded


def register_litellm_models(git_root, model_metadata_fname, verbose=False):
    model_metadata_files = []

    # Add the resource file path
    resource_metadata = importlib_resources.files("aider.resources").joinpath("model-metadata.json")
    model_metadata_files.append(str(resource_metadata))

    model_metadata_files += generate_search_path_list(
        ".aider.model.metadata.json", git_root, model_metadata_fname
    )

    try:
        model_metadata_files_loaded = models.register_litellm_models(model_metadata_files)
        if len(model_metadata_files_loaded) > 0 and verbose:
            print("Loaded model metadata from:")
            for model_metadata_file in model_metadata_files_loaded:
                print(f"  - {model_metadata_file}")  # noqa: E221
    except Exception as e:
        print(f"Error loading model metadata models: {e}")
        return 1


def sanity_check_repo(repo):
    if not repo:
        return True

    if not repo.repo.working_tree_dir:
        print("The git repo does not seem to have a working tree?")
        return False

    bad_ver = False
    try:
        repo.get_tracked_files()
        if not repo.git_repo_error:
            return True
        error_msg = str(repo.git_repo_error)
    except UnicodeDecodeError as exc:
        error_msg = (
            "Failed to read the Git repository. This issue is likely caused by a path encoded "
            f'in a format different from the expected encoding "{sys.getfilesystemencoding()}".\n'
            f"Internal error: {str(exc)}"
        )
    except ANY_GIT_ERROR as exc:
        error_msg = str(exc)
        bad_ver = "version in (1, 2)" in error_msg
    except AssertionError as exc:
        error_msg = str(exc)
        bad_ver = True

    if bad_ver:
        print("Aider only works with git repos with version number 1 or 2.")
        print("You may be able to convert your repo: git update-index --index-version=2")
        print("Or run aider --no-git to proceed without using git.")
        print("Open documentation url for more info?")
        return False

    print("Unable to read git repository, it may be corrupt?")
    print(error_msg)
    return False


def main(argv=None, input=None, output=None, force_git_root=None, return_coder=False):
    report_uncaught_exceptions()

    if git is None:
        git_root = None
    elif force_git_root:
        git_root = force_git_root
    else:
        git_root = get_git_root()

    conf_fname = Path(".aider.conf.yml")

    default_config_files = []
    try:
        default_config_files += [conf_fname.resolve()]  # CWD
    except OSError:
        pass

    if git_root:
        git_conf = Path(git_root) / conf_fname  # git root
        if git_conf not in default_config_files:
            default_config_files.append(git_conf)
    default_config_files.append(Path.home() / conf_fname)  # homedir
    default_config_files = list(map(str, default_config_files))

    if len(all_files) > 1:
        good = True
        for fname in all_files:
            if Path(fname).is_dir():
                print(f"{fname} is a directory, not provided alone.")
                good = False
        if not good:
            print(
                "Provide either a single directory of a git repo, or a list of one or more files."
            )
            return 1

    git_dname = None
    if len(all_files) == 1:
        if Path(all_files[0]).is_dir():
            if args.git:
                git_dname = str(Path(all_files[0]).resolve())
                fnames = []
            else:
                print(f"{all_files[0]} is a directory, but --no-git selected.")
                return 1

    # We can't know the git repo for sure until after parsing the args.
    # If we guessed wrong, reparse because that changes things like
    # the location of the config.yml and history files.
    if args.git and not force_git_root and git is not None:
        right_repo_root = guessed_wrong_repo(git_root, fnames, git_dname)
        if right_repo_root:
            return main(argv, input, output, right_repo_root, return_coder=return_coder)

    try:
        coder = Coder.create(
            main_model=main_model,
            edit_format=args.edit_format,
            repo=repo,
            fnames=fnames,
            read_only_fnames=read_only_fnames,
            show_diffs=args.show_diffs,
            auto_commits=args.auto_commits,
            dirty_commits=args.dirty_commits,
            dry_run=args.dry_run,
            map_tokens=map_tokens,
            verbose=args.verbose,
            stream=args.stream,
            use_git=args.git,
            restore_chat_history=args.restore_chat_history,
            auto_lint=args.auto_lint,
            auto_test=args.auto_test,
            lint_cmds=lint_cmds,
            test_cmd=args.test_cmd,
            commands=commands,
            summarizer=summarizer,
            map_refresh=args.map_refresh,
            cache_prompts=args.cache_prompts,
            map_mul_no_files=args.map_multiplier_no_files,
            num_cache_warming_pings=args.cache_keepalive_pings,
            suggest_shell_commands=args.suggest_shell_commands,
            chat_language=args.chat_language,
            detect_urls=args.detect_urls,
            auto_copy_context=args.copy_paste,
        )
    except UnknownEditFormat as err:
        print(str(err))
        print("Open documentation about edit formats?")
        return 1
    except ValueError as err:
        print(str(err))
        return 1

    if return_coder:
        return coder

  ignores = []
    if git_root:
        ignores.append(str(Path(git_root) / ".gitignore"))

    if git_root and Path.cwd().resolve() != Path(git_root).resolve():
        print(
            "Note: in-chat filenames are always relative to the git working dir, not the current"
            " working dir."
        )

        print(f"Cur working dir: {Path.cwd()}")
        print(f"Git working dir: {git_root}")
    while True:
        try:
            coder.run()
            return
        except SwitchCoder as switch:
            kwargs = dict(from_coder=coder)
            kwargs.update(switch.kwargs)
            if "show_announcements" in kwargs:
                del kwargs["show_announcements"]

            coder = Coder.create(**kwargs)

            if switch.kwargs.get("show_announcements") is not False:
                coder.show_announcements()


def is_first_run_of_new_version(verbose=False):
    """Check if this is the first run of a new version/executable combination"""
    installs_file = Path.home() / ".aider" / "installs.json"
    key = (__version__, sys.executable)

    # Never show notes for .dev versions
    if ".dev" in __version__:
        return False

    if verbose:
        print(
            f"Checking imports for version {__version__} and executable {sys.executable}"
        )
        print(f"Installs file: {installs_file}")

    try:
        if installs_file.exists():
            with open(installs_file, "r") as f:
                installs = json.load(f)
            if verbose:
                print("Installs file exists and loaded")
        else:
            installs = {}
            if verbose:
                print("Installs file does not exist, creating new dictionary")

        is_first_run = str(key) not in installs

        if is_first_run:
            installs[str(key)] = True
            installs_file.parent.mkdir(parents=True, exist_ok=True)
            with open(installs_file, "w") as f:
                json.dump(installs, f, indent=4)

        return is_first_run

    except Exception as e:
        print(f"Error checking version: {e}")
        if verbose:
            print(f"Full exception details: {traceback.format_exc()}")
        return True  # Safer to assume it's a first run if we hit an error


def check_and_load_imports(is_first_run, verbose=False):
    try:
        if is_first_run:
            if verbose:
                print(
                    "First run for this version and executable, loading imports synchronously"
                )
            try:
                load_slow_imports(swallow=False)
            except Exception as err:
                print(str(err))
                print("Error loading required imports. Did you install aider properly?")
                print("Open documentation url for more info?")
                sys.exit(1)

            if verbose:
                print("Imports loaded and installs file updated")
        else:
            if verbose:
                print("Not first run, loading imports in background thread")
            thread = threading.Thread(target=load_slow_imports)
            thread.daemon = True
            thread.start()

    except Exception as e:
        print(f"Error in loading imports: {e}")
        if verbose:
            print(f"Full exception details: {traceback.format_exc()}")


def load_slow_imports(swallow=True):
    # These imports are deferred in various ways to
    # improve startup time.
    # This func is called either synchronously or in a thread
    # depending on whether it's been run before for this version and executable.

    try:
        import httpx  # noqa: F401
        import litellm  # noqa: F401
        import networkx  # noqa: F401
        import numpy  # noqa: F401
    except Exception as e:
        if not swallow:
            raise e


if __name__ == "__main__":
    status = main()
    sys.exit(status)
