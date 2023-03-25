import argparse
import copy
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import typing
import uuid

import event_stream
import jsonschema
import toml

import animals
import filters.arbiter_saturation
import filters.default
import tasks.colourtime
import tasks.event_rate
import tasks.video

parser = argparse.ArgumentParser(
    description="Process Event Stream files",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
subparsers = parser.add_subparsers(dest="command")
configure_parser = subparsers.add_parser(
    "configure", help="Generate a configuration file"
)
configure_parser.add_argument(
    "directory",
    help="Directory to scan (recursively) for Event Stream files",
)
configure_parser.add_argument(
    "--configuration",
    "-c",
    default="render-configuration.toml",
    help="Render configuration file path",
)
configure_parser.add_argument(
    "--force",
    "-f",
    action="store_true",
    help="Replace the configuration if it exists",
)
run_parser = subparsers.add_parser("run", help="Process a configuration file")
run_parser.add_argument(
    "--configuration",
    "-c",
    default="render-configuration.toml",
    help="Render configuration file path",
)
run_parser.add_argument(
    "--force",
    "-f",
    action="store_true",
    help="Replace files that already exist",
)
resolve_parser = subparsers.add_parser("resolve", help="Apply generators for debugging")
resolve_parser.add_argument(
    "--configuration",
    "-c",
    default="render-configuration.toml",
    help="Render configuration file path",
)
resolve_parser.add_argument(
    "--output",
    "-o",
    default="render-configuration-resolved.json",
    help="Resolved render configuration file path",
)
args = parser.parse_args()


TIMECODE_PATTERN = re.compile(r"^(\d+):(\d+):(\d+)(?:\.(\d+))?$")

filter_apply = typing.Callable[
    [
        pathlib.Path,
        pathlib.Path,
        int,
        int,
        dict[str, typing.Any],
    ],
    None,
]

FILTERS: dict[str, filter_apply] = {
    "default": filters.default.apply,
    "arbiter_saturation": filters.arbiter_saturation.apply,
}

task_run = typing.Callable[
    [
        pathlib.Path,
        pathlib.Path,
        int,
        int,
        dict[str, typing.Any],
    ],
    None,
]

TASKS: dict[str, tuple[str, task_run]] = {
    "colourtime": (tasks.colourtime.EXTENSION, tasks.colourtime.run),
    "event_rate": (tasks.event_rate.EXTENSION, tasks.event_rate.run),
    "video": (tasks.video.EXTENSION, tasks.video.run),
}

ANSI_COLORS_ENABLED = os.getenv("ANSI_COLORS_DISABLED") is None


def format_bold(message: str) -> str:
    if ANSI_COLORS_ENABLED:
        return f"\033[1m{message}\033[0m"
    return message


def info(icon: str, message: str):
    sys.stdout.write(f"{icon} {message}\n")
    sys.stdout.flush()


def error(message: str):
    sys.stderr.write(f"❌ {message}\n")
    sys.exit(1)


def timestamp_to_timecode(timestamp: int):
    hours = timestamp // (60 * 60 * 1000000)
    timestamp -= hours * 60 * 60 * 1000000
    minutes = timestamp // (60 * 1000000)
    timestamp -= minutes * 60 * 1000000
    seconds = timestamp // 1000000
    timestamp -= seconds * 1000000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{timestamp:06d}"


def timestamp_to_short_timecode(timestamp: int):
    hours = timestamp // (60 * 60 * 1000000)
    timestamp -= hours * 60 * 60 * 1000000
    minutes = timestamp // (60 * 1000000)
    timestamp -= minutes * 60 * 1000000
    seconds = timestamp // 1000000
    timestamp -= seconds * 1000000
    timestamp_string = "" if timestamp == 0 else f".{timestamp:06d}".rstrip("0")
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}{timestamp_string}"
    if minutes > 0:
        return f"{minutes}:{seconds:02d}{timestamp_string}"
    return f"{seconds}{timestamp_string}"


def timecode(value: str) -> int:
    if value.isdigit():
        return int(value)
    match = TIMECODE_PATTERN.match(value)
    if match is None:
        raise argparse.ArgumentTypeError(
            f"expected an integer or a timecode (12:34:56.789000), got {value}"
        )
    result = (
        int(match[1]) * 3600000000 + int(match[2]) * 60000000 + int(match[3]) * 1000000
    )
    if match[4] is not None:
        fraction_string: str = match[4]
        if len(fraction_string) == 6:
            result += int(fraction_string)
        elif len(fraction_string) < 6:
            result += int(fraction_string + "0" * (6 - len(fraction_string)))
        else:
            result += round(float("0." + fraction_string) * 1e6)
    return result


class Encoder(toml.TomlEncoder):
    def dump_list(self, v):
        return f"[{', '.join(str(self.dump_value(u)) for u in v)}]"

    def dump_sections(self, o, sup):
        retstr = ""
        if sup != "" and sup[-1] != ".":
            sup += "."
        retdict = self._dict()
        arraystr = ""
        for section in o:
            section = str(section)
            qsection = section
            if not re.match(r"^[A-Za-z0-9_-]+$", section):
                qsection = toml.encoder._dump_str(section)  # type: ignore
            if not isinstance(o[section], dict):
                arrayoftables = False
                if isinstance(o[section], list):
                    for a in o[section]:
                        if isinstance(a, dict):
                            arrayoftables = True
                if arrayoftables:
                    for index, a in enumerate(o[section]):
                        arraytabstr = ""
                        arraystr += f"\n[[{sup}{qsection}]]\n"
                        s, d = self.dump_sections(a, sup + qsection)
                        if s:
                            if s[0] == "[":
                                arraytabstr += s
                            else:
                                arraystr += s
                        while d:
                            newd = self._dict()
                            for dsec in d:
                                s1, d1 = self.dump_sections(
                                    d[dsec], sup + qsection + "." + dsec
                                )
                                if s1:
                                    arraytabstr += (
                                        "[" + sup + qsection + "." + dsec + "]\n"
                                    )
                                    arraytabstr += s1
                                for s1 in d1:
                                    newd[dsec + "." + s1] = d1[s1]
                            d = newd
                        arraystr += arraytabstr
                else:
                    if o[section] is not None:
                        retstr += (
                            qsection + " = " + str(self.dump_value(o[section])) + "\n"
                        )
            else:
                retdict[qsection] = o[section]
        retstr += arraystr
        return (retstr, retdict)


def render_configuration_schema():
    with open(
        pathlib.Path(__file__).resolve().parent / "render-configuration-schema.json"
    ) as schema_file:
        return json.load(schema_file)


def with_suffix(path: pathlib.Path, suffix: str):
    return path.parent / f"{path.name}{suffix}"


def load_parameters(path: pathlib.Path):
    if path.is_file():
        with open(path) as file:
            return toml.load(file)
    return None


def save_parameters(path: pathlib.Path, parameters: dict[str, typing.Any]):
    with open(path.with_suffix(".part"), "w") as file:
        toml.dump(parameters, file)
    path.with_suffix(".part").rename(path)


def compare_parameters(a: dict[str, typing.Any], b: dict[str, typing.Any]):
    return json.dumps(a, sort_keys=True, separators=(",", ":")) == json.dumps(
        b, sort_keys=True, separators=(",", ":")
    )


def recursive_replace(
    template: dict[str, typing.Any], parameter_name: str, parameter_value: typing.Any
):
    for key, value in template.items():
        if isinstance(value, str):
            if value == f"@raw({parameter_name})":
                template[key] = parameter_value
            else:
                template[key] = value.replace(
                    f"@{parameter_name}", str(parameter_value)
                )
        elif isinstance(value, dict):
            recursive_replace(
                template=value,
                parameter_name=parameter_name,
                parameter_value=parameter_value,
            )


def run_generators(configuration: dict[str, typing.Any]):
    for key, generator_key in (
        ("filters", "filters-generators"),
        ("tasks", "tasks-generators"),
        ("jobs", "jobs-generators"),
    ):
        if generator_key in configuration:
            for generator in configuration[generator_key]:
                values_counts = [
                    len(values) for values in generator["parameters"].values()
                ]
                if len(values_counts) == 0:
                    error(
                        f"{key} generator \"{generator['template']['name']}\" has no parameters"
                    )
                if not all(
                    values_count == values_counts[0] for values_count in values_counts
                ):
                    error(
                        f"the parameters in {key} generator \"{generator['template']['name']}\" have different numbers of values"
                    )
                parameters_names_and_values = sorted(
                    generator["parameters"].items(),
                    key=lambda key_and_value: -len(key_and_value[0]),
                )
                for parameters_values in zip(
                    *(values for _, values in parameters_names_and_values)
                ):
                    generated_entry = copy.deepcopy(generator["template"])
                    if key == "jobs":
                        generated_entry_name = None
                    else:
                        generated_entry_name = generated_entry["name"]
                        del generated_entry["name"]
                    for parameter_name, parameter_value in zip(
                        (name for name, _ in parameters_names_and_values),
                        parameters_values,
                    ):
                        if key != "jobs":
                            assert generated_entry_name is not None
                            generated_entry_name = generated_entry_name.replace(
                                f"@{parameter_name}", str(parameter_value)
                            )
                        recursive_replace(
                            template=generated_entry,
                            parameter_name=parameter_name,
                            parameter_value=parameter_value,
                        )
                    if key == "jobs":
                        configuration[key].append(generated_entry)
                    else:
                        if generated_entry_name in configuration[key]:
                            error(
                                f"the {key} generator \"{generator['template']['name']}\" created an entry whose name (\"{generated_entry_name}\") already exists"
                            )
                        configuration[key][generated_entry_name] = generated_entry
            del configuration[generator_key]


if args.command == "configure":
    configuration_path = pathlib.Path(args.configuration).resolve()
    if not args.force and configuration_path.is_file():
        error(f'"{configuration_path}" already exists (use --force to override it)')
    directory = pathlib.Path(args.directory).resolve()
    if not directory.is_dir():
        error(f'"{directory}" does not exist or is not a directory')
    paths = list(directory.rglob("*.es"))
    paths.sort(key=lambda path: (path.stem, path.parent))
    if len(paths) == 0:
        error(f'no .es files found in "{directory}"')
    names = animals.generate_names(len(paths))
    attachments: dict[str, list[dict[str, str]]] = {}
    for name, path in zip(names, paths):
        for sibling in path.parent.iterdir():
            if sibling != path and sibling.stem == path.stem:
                if not name in attachments:
                    attachments[name] = []
                attachments[name].append(
                    {"source": str(sibling), "target": f"{name}{sibling.suffix}"}
                )
    jobs = []
    for index, (name, path) in enumerate(zip(names, paths)):
        info(
            animals.composite_name_to_icon(name),
            f'{index + 1}/{len(paths)} reading range for {format_bold(name)} ("{path}")',
        )
        begin: typing.Optional[int] = None
        end: typing.Optional[int] = None
        with event_stream.Decoder(path) as decoder:
            for packet in decoder:
                if begin is None:
                    begin = int(packet["t"][0])
                end = int(packet["t"][-1])
        if begin is None:
            begin = 0
            end = begin + 1
        else:
            assert end is not None
            end += 1
        jobs.append(
            {
                "name": name,
                "begin": timestamp_to_timecode(begin),
                "end": timestamp_to_timecode(end),
                "filters": ["default"],
                "tasks": ["colourtime-.+", "event-rate-.+", "video-real-time"],
            }
        )
    with open(with_suffix(configuration_path, ".part"), "w") as configuration_file:
        configuration_file.write("# output directory\n")
        toml.dump({"directory": "renders"}, configuration_file, encoder=Encoder())
        configuration_file.write(
            "\n\n# filters configuration (filters are applied before tasks)\n\n"
        )
        toml.dump(
            {
                "filters": {
                    "default": {"type": "default", "icon": "⏳", "suffix": ""},
                }
            },
            configuration_file,
            encoder=Encoder(),
        )
        configuration_file.write(
            "\n\n# filters generators (advanced filter generation with templates)\n"
        )
        toml.dump(
            {
                "filters-generators": [
                    {
                        "parameters": {
                            "threshold": [1, 5, 10, 15, 30, 45, 90, 180, 360, 720],
                        },
                        "template": {
                            "name": "arbiter-saturation-@threshold",
                            "type": "arbiter_saturation",
                            "icon": "🌩 ",
                            "suffix": "as@threshold",
                            "threshold": "@raw(threshold)",
                        },
                    },
                    {
                        "parameters": {
                            "ratio": [1.0, 2.0, 3.0, 5.0, 10.0],
                        },
                        "template": {
                            "name": "hot-pixels-@ratio",
                            "type": "hot_pixels",
                            "icon": "🌶",
                            "suffix": "hp@ratio",
                            "ratio": "@raw(ratio)",
                        },
                    },
                ]
            },
            configuration_file,
            encoder=Encoder(),
        )

        configuration_file.write("\n\n# tasks configuration\n\n")
        toml.dump(
            {
                "tasks": {
                    "video-real-time": {
                        "type": "video",
                        "icon": "🎬",
                        "frametime": timestamp_to_timecode(20000),
                        "tau": timestamp_to_timecode(200000),
                        "style": "exponential",
                        "on_color": "#F4C20D",
                        "off_color": "#1E88E5",
                        "idle_color": "#191919",
                        "cumulative_ratio": 0.01,
                        "timecode": True,
                        "h264_crf": 15,
                        "ffmpeg": "ffmpeg",
                    },
                },
            },
            configuration_file,
            encoder=Encoder(),
        )

        configuration_file.write(
            "\n\n# tasks generators (advanced task generation with templates)\n"
        )
        toml.dump(
            {
                "tasks-generators": [
                    {
                        "parameters": {
                            "colormap": ["viridis", "prism"],
                        },
                        "template": {
                            "name": "colourtime-@colormap",
                            "type": "colourtime",
                            "icon": "🎨",
                            "colormap": "@colormap",
                            "alpha": 0.1,
                            "png_compression_level": 6,
                            "background_color": "#191919",
                        },
                    },
                    {
                        "parameters": {
                            "suffix": ["100000-10000", "1000-100"],
                            "long_tau": [
                                timestamp_to_timecode(100000),
                                timestamp_to_timecode(10000),
                            ],
                            "short_tau": [
                                timestamp_to_timecode(1000),
                                timestamp_to_timecode(100),
                            ],
                        },
                        "template": {
                            "name": "event-rate-@suffix",
                            "type": "event_rate",
                            "icon": "🎢",
                            "long_tau": "@long_tau",
                            "short_tau": "@short_tau",
                            "long_tau_color": "#4285F4",
                            "short_tau_color": "#C4D7F5",
                            "axis_color": "#000000",
                            "main_grid_color": "#555555",
                            "secondary_grid_color": "#DDDDDD",
                            "width": 1920,
                            "height": 1080,
                        },
                    },
                ]
            },
            configuration_file,
            encoder=Encoder(),
        )

        configuration_file.write(
            "\n\n# jobs (source + filters + tasks)\n# the same source file can be used in multiple jobs if begin, end, or filters are different\n#\n"
        )
        toml.dump(
            {"jobs": jobs},
            configuration_file,
            encoder=Encoder(),
        )
        configuration_file.write(
            "\n\n# jobs generators (advanced job generation with templates)\n#\n"
        )
        configuration_file.write(
            "\n".join(
                f"# {line}"
                for line in toml.dumps(
                    {
                        "jobs-generators": [
                            {
                                "parameters": {
                                    "threshold": [
                                        1,
                                        5,
                                        10,
                                        15,
                                        30,
                                        45,
                                        90,
                                        180,
                                        360,
                                        720,
                                    ],
                                },
                                "template": {
                                    "name": "job-name",
                                    "begin": "job-begin",
                                    "end": "job-end",
                                    "filters": ["arbiter-saturation-@threshold"],
                                    "tasks": [
                                        "colourtime-.+",
                                        "event-rate-.+",
                                        "video-real-time",
                                    ],
                                },
                            }
                        ]
                    },
                    encoder=Encoder(),
                ).split("\n")
                if len(line) > 0
            )
        )
        configuration_file.write("\n\n# generated name to source file\n")
        toml.dump(
            {"sources": {name: str(path) for name, path in zip(names, paths)}},
            configuration_file,
            encoder=Encoder(),
        )
        configuration_file.write(
            "\n\n# attachments are copied in target directories, algonside generated files \n"
        )
        toml.dump(
            {"attachments": attachments},
            configuration_file,
            encoder=Encoder(),
        )
    with open(with_suffix(configuration_path, ".part")) as configuration_file:
        jsonschema.validate(
            toml.load(configuration_file),
            render_configuration_schema(),
        )
    with_suffix(configuration_path, ".part").rename(configuration_path)
    sys.exit(0)

if args.command == "run":
    configuration_path = pathlib.Path(args.configuration).resolve()
    with open(configuration_path) as configuration_file:
        configuration = toml.load(configuration_file)
    jsonschema.validate(configuration, render_configuration_schema())
    run_generators(configuration)
    jsonschema.validate(configuration, render_configuration_schema())
    for job in configuration["jobs"]:
        if not job["name"] in configuration["sources"]:
            error(f"\"{job['name']}\" is not listed in sources")
        for filter in job["filters"]:
            if not filter in configuration["filters"]:
                error(f"unknown filter \"{filter}\" in \"{job['name']}\"")
        if "tasks" in job:
            expanded_tasks = []
            for task in job["tasks"]:
                pattern = re.compile(task)
                found = False
                for task_name in configuration["tasks"].keys():
                    if pattern.fullmatch(task_name) is not None:
                        expanded_tasks.append(task_name)
                        found = True
                if not found in configuration["tasks"]:
                    error(
                        f"\"{task}\" in \"{job['name']}\" did not match any task names"
                    )
        try:
            timecode(job["begin"])
        except Exception as exception:
            error(
                f"parsing \"begin\" ({job['begin']}) in \"{job['name']}\" failed ({exception})"
            )
        try:
            timecode(job["end"])
        except Exception as exception:
            error(
                f"parsing \"end\" ({job['end']}) in \"{job['name']}\" failed ({exception})"
            )
    for name, attachment in configuration["attachments"].items():
        targets = [file["target"] for file in attachment]
        if len(targets) != len(set(targets)):
            error(f'two or more attachments share the same target in "{name}"')
    configuration["filters"] = {
        name: {
            "type": filter["type"],
            "icon": filter["icon"],
            "suffix": filter["suffix"],
            "parameters": {
                key: value
                for key, value in filter.items()
                if key != "type" and key != "icon" and key != "suffix"
            },
        }
        for name, filter in configuration["filters"].items()
    }
    configuration["tasks"] = {
        name: {
            "type": task["type"],
            "icon": task["icon"],
            "parameters": {
                key: value
                for key, value in task.items()
                if key != "type" and key != "icon"
            },
        }
        for name, task in configuration["tasks"].items()
    }
    directory = pathlib.Path(configuration["directory"])
    if directory.is_absolute():
        directory = directory.resolve()
    else:
        directory = (configuration_path.parent / directory).resolve()
    info("📁", f'output directory "{directory}"\n')
    directory.mkdir(parents=True, exist_ok=True)
    for index, job in enumerate(configuration["jobs"]):
        begin = timecode(job["begin"])
        end = timecode(job["end"])
        name = f"{job['name']}-b{timestamp_to_short_timecode(begin)}-e{timestamp_to_short_timecode(end)}"
        source = configuration["sources"][job["name"]]
        if "filters" in job and len(job["filters"]) > 0:
            for filter_name in job["filters"]:
                if len(configuration["filters"][filter_name]["suffix"]) > 0:
                    name += f'-{configuration["filters"][filter_name]["suffix"]}'
        (directory / name).mkdir(exist_ok=True)
        info(
            animals.composite_name_to_icon(job["name"]),
            f"{index + 1}/{len(configuration['jobs'])} {format_bold(name)}",
        )
        output_path = directory / name / f"{name}.es"
        parameters_path = directory / name / "parameters.toml"
        parameters = load_parameters(parameters_path)
        if parameters is None:
            parameters = {}
        if not "source" in parameters or parameters["source"] != source:
            parameters = {"source": source}
            save_parameters(parameters_path, parameters)
        if not "filters" in parameters:
            parameters["filters"] = {}
        if not "tasks" in parameters:
            parameters["tasks"] = {}
        if not "attachments" in parameters:
            parameters["attachments"] = {}
        for attachment in configuration["attachments"]:
            if (
                not args.force
                and attachment["target"] in parameters["attachments"]
                and (directory / name / attachment["target"]).is_file()
            ):
                info(
                    "⏭ ", f"skip copy {attachment['source']} to {attachment['target']}"
                )
            else:
                info("🗃", f"copy {attachment['source']} to {attachment['target']}")
                shutil.copy2(
                    pathlib.Path(attachment["source"]),
                    with_suffix(directory / name / attachment["target"], ".part"),
                )
                with_suffix(directory / name / attachment["target"], ".part").rename(
                    directory / name / attachment["target"]
                )
            parameters["attachments"][attachment["target"]] = attachment["source"]
        if len(job["filters"]) == 1:
            filter_name = job["filters"][0]
            filter = configuration["filters"][filter_name]
            if (
                not args.force
                and filter_name in parameters["filters"]
                and compare_parameters(
                    parameters["filters"][filter_name], filter["parameters"]
                )
                and output_path.is_file()
            ):
                info("⏭ ", f"skip filter {filter_name}")
            else:
                info(filter["icon"], f"apply filter {filter_name}")
                FILTERS[filter["type"]](
                    pathlib.Path(configuration["sources"][job["name"]]),
                    with_suffix(output_path, ".part"),
                    begin,
                    end,
                    filter["parameters"],
                )
                with_suffix(output_path, ".part").rename(output_path)
                parameters["filters"][filter_name] = filter["parameters"]
                save_parameters(parameters_path, parameters)
        else:
            if (
                not args.force
                and all(
                    (
                        filter_name in parameters["filters"]
                        and compare_parameters(
                            parameters["filters"][filter_name],
                            configuration["filters"][filter_name]["parameters"],
                        )
                    )
                    for filter_name in job["filters"]
                )
                and output_path.is_file()
            ):
                info("⏭ ", f"skip filters {' + '.join(job['filters'])}")
            else:
                with tempfile.TemporaryDirectory(
                    suffix=job["name"]
                ) as temporary_directory_name:
                    temporary_directory = pathlib.Path(temporary_directory_name)
                    input = pathlib.Path(configuration["sources"][job["name"]])
                    for index, filter_name in enumerate(job["filters"]):
                        if index == len(job["filters"]) - 1:
                            output = with_suffix(output_path, ".part")
                        else:
                            output = temporary_directory / f"{uuid.uuid4()}.es"
                        filter = configuration["filters"][filter_name]
                        info(filter["icon"], f"apply filter {filter_name}")
                        FILTERS[filter["type"]](
                            input,
                            output,
                            begin,
                            end,
                            filter["parameters"],
                        )
                        input = output
                        parameters["filters"][filter_name] = filter["parameters"]
                with_suffix(output_path, ".part").rename(output_path)
                save_parameters(parameters_path, parameters)
        for task_name in job["tasks"]:
            task = configuration["tasks"][task_name]
            task_output_path = (
                directory / name / f"{name}-{task_name}{TASKS[task['type']][0]}"
            )
            if (
                not args.force
                and task_name in parameters["tasks"]
                and compare_parameters(
                    parameters["tasks"][task_name], task["parameters"]
                )
                and output_path.is_file()
            ):
                info("⏭ ", f"skip task {task_name}")
            else:
                info(task["icon"], f"run task {task_name}")
                TASKS[task["type"]][1](
                    output_path,
                    with_suffix(task_output_path, ".part"),
                    begin,
                    end,
                    task["parameters"],
                )
                with_suffix(task_output_path, ".part").rename(task_output_path)
                parameters["tasks"][task_name] = task["parameters"]
                save_parameters(parameters_path, parameters)

        if index < len(configuration["jobs"]) - 1:
            sys.stdout.write("\n")
    sys.exit(0)

if args.command == "resolve":
    configuration_path = pathlib.Path(args.configuration).resolve()
    with open(configuration_path) as configuration_file:
        configuration = toml.load(configuration_file)
    jsonschema.validate(configuration, render_configuration_schema())
    run_generators(configuration)
    jsonschema.validate(configuration, render_configuration_schema())
    with open(pathlib.Path(args.output), "w") as output_file:
        json.dump(configuration, output_file, indent=4)
