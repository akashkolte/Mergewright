Handling CTE at the start of query in DML
Given that query, I expect that `model` should be in `cte` section. 
Is it some kind of reserved word?

```sql
INSERT INTO db.table1 ("date")
WITH raw AS (
  SELECT
    date
  FROM db.table2
), model AS (
  SELECT
    raw.date
  FROM raw
  LEFT JOIN db.table3 USING (date)
)
SELECT * FROM model

table read: [Table: <default>.model, Table: db.table2, Table: db.table3]
table write: [Table: db.table1]
table cte: []
```

When there is only one CTE it works correctly
```sql
INSERT INTO db.table1 ("date")
WITH model AS (
  SELECT
    db.date
  FROM db.table2
)
SELECT * FROM model

table read: [Table: db.table2]
table write: [Table: db.table1]
table cte: [SubQuery: model]
```

If I wrap `raw` in backticks it also works

```sql
INSERT INTO db.table1 ("date")
WITH `raw` AS (
  SELECT
    date
  FROM db.table2
), model AS (
  SELECT
    raw.date
  FROM raw
  LEFT JOIN db.table3
    USING (date)
)
SELECT * FROM model

table read: [Table: db.table2, Table: db.table3]
table write: [Table: db.table1]
table cte: [SubQuery: `raw`, SubQuery: model]
```

versions that I use:
```
sqllineage               1.3.7
sqlparse                 0.4.1
```