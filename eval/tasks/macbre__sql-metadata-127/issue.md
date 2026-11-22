sql_metadata.get_query_columns  result error 
demo
```
sql_metadata.get_query_columns("select max(col3)+avg(col)+1+sum(col2) from dual")
// result :  ['col3', 'avg', 'col', 'sum', 'col2'].   avg sum  shouldn't be a column

sql_metadata.get_query_columns("select avg(col)+sum(col2) from dual")
// result: ['avg', 'col', 'sum', 'col2']  avg sum  shouldn't be a column

sql_metadata.get_query_columns("select count(col)+max(col2)+ min(col3)+ count(distinct  col4) + custom_func(col5) from dual")
// result ['col', 'col2', 'col3', 'col4', 'custom_func', 'col5']   custom_func shouldn't be a column
```
it looks like function matching by keyword, I recommend matching by function pattern
