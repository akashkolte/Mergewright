`LocalPathExpander` should match only `folder_paths` or `file_paths` depending on the spec.
I am trying to use the `LocalPathExpander` for the Clandinin project but I am encountering the following issue. I have something like the following structure:

```python
.
└── base_directory
    ├── a_simple_pattern_1
    │   └── a_simple_pattern_1.bin
    └── a_simple_pattern_2
        └── a_simple_pattern_2.bin


```

Then I call:

```python
from neuroconv.tools.path_expansion import LocalPathExpander

# Specify source data (note this assumes the files are arranged in the same way as in the example data)
source_data_spec = {
    "a_source": {
        "base_directory": base_directory,
        "folder_path": "a_simple_pattern_{session_id}",
    }
}

path_expander = LocalPathExpander()
metadata_list = path_expander.expand_paths(source_data_spec)

folder_paths = [
    metadata_match["source_data"]["a_source"]["folder_path"] for metadata_match in metadata_list
]
folder_paths
```

My expectation would be to have only:
```python
["tmp/base_directory/a_simple_pattern_2", "tmp/base_directory/a_simple_pattern_1"]
```
But instead I get:

```python
[
    "tmp/base_directory/a_simple_pattern_2",
    "tmp/base_directory/a_simple_pattern_1",
    "tmp/base_directory/a_simple_pattern_2/a_simple_pattern_2.bin",
    "tmp/base_directory/a_simple_pattern_1/a_simple_pattern_1.bin",
]
```

That is, the `LocalPathExpander` is matching both `folder_paths` and `file_paths` when I am only requesting folder_paths. This makes it harder for me to use `LocalPathExpander` as a source of arguments for defining my interface as I am getting extra terms in the resulting dictionary that I need to filter after.

@bendichter @CodyCBakerPhD 
What do you think guys? is this a feature or is this a bug? I feel that if you specificy `folder_path` in the spec you should only get folder back and the same for file_paths. I made an implementation and more complete tests on https://github.com/catalystneuro/neuroconv/pull/675
 