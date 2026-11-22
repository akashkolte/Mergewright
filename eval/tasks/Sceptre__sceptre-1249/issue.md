Sceptre fails when multiple resolvers return None
### Sceptre fails when multiple resolvers return None
When multiple resolvers in a stack config return None sceptre fails.

### Your environment
* version of sceptre (3.1.0)
* version of python (3.8.10)
* which OS/distro -- windows 10, linux

### Steps to reproduce
```
sceptre_user_data:
  some_structure:
    abc0: !resolver1
    abc1: !resolver2
    abc3: !resolver3
```

### Expected behaviour
Variables in the stack should get None (the actual values that the resolvers returned)

### Actual behaviour
sceptre fails with
```
Traceback (most recent call last):

  File "scripts/render-templates.py", line 73, in main

    for stack, template_body in plan.generate().items():

  File "C:\Users\ykhalyavin\venv\cfn\lib\site-packages\sceptre\plan\plan.py", line 351, in generate

    return self._execute(*args)

  File "C:\Users\ykhalyavin\venv\cfn\lib\site-packages\sceptre\plan\plan.py", line 43, in _execute

    return executor.execute(*args)

  File "C:\Users\ykhalyavin\venv\cfn\lib\site-packages\sceptre\plan\executor.py", line 55, in execute

    stack, status = future.result()

  File "C:\Users\ykhalyavin\AppData\Local\Programs\Python\Python38\lib\concurrent\futures\_base.py", line 432, in result

    return self.__get_result()

  File "C:\Users\ykhalyavin\AppData\Local\Programs\Python\Python38\lib\concurrent\futures\_base.py", line 388, in __get_result

    raise self._exception

  File "C:\Users\ykhalyavin\AppData\Local\Programs\Python\Python38\lib\concurrent\futures\thread.py", line 57, in run

    result = self.fn(*self.args, **self.kwargs)

  File "C:\Users\ykhalyavin\venv\cfn\lib\site-packages\sceptre\plan\executor.py", line 62, in _execute

    result = getattr(actions, self.command)(*args)

  File "C:\Users\ykhalyavin\venv\cfn\lib\site-packages\sceptre\hooks\__init__.py", line 104, in decorated

    response = func(self, *args, **kwargs)

  File "C:\Users\ykhalyavin\venv\cfn\lib\site-packages\sceptre\plan\actions.py", line 634, in generate

    return self.stack.template.body

  File "C:\Users\ykhalyavin\venv\cfn\lib\site-packages\sceptre\stack.py", line 352, in template

    sceptre_user_data=self.sceptre_user_data,

  File "C:\Users\ykhalyavin\venv\cfn\lib\site-packages\sceptre\resolvers\__init__.py", line 191, in __get__

    container = super().__get__(stack, stack_class)

  File "C:\Users\ykhalyavin\venv\cfn\lib\site-packages\sceptre\resolvers\__init__.py", line 93, in __get__

    return self.get_resolved_value(stack, stack_class)

  File "C:\Users\ykhalyavin\venv\cfn\lib\site-packages\sceptre\resolvers\__init__.py", line 245, in get_resolved_value

    del attr[key]

IndexError: list assignment index out of rangeTraceback (most recent call last):
```
