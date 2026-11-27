Handle optional args in query cost multiplers
Query cost validator supports complexity multipliers which multiply given complexity by sum of specified values:

```graphql
type Query {
    news(promoted: Int, regular: Int): Post @cost(complexity: 1, multipliers: ["promoted", "regular"])
}
```

In situation when one of those arguments is skipped, its passed to complexity calculation as `None`, causing the `unsupported operand type(s) for +: 'int' and 'NoneType'` error within validator.

Relevant lines:

https://github.com/mirumee/ariadne/blob/master/ariadne/validation/query_cost.py#L283
https://github.com/mirumee/ariadne/blob/master/ariadne/validation/query_cost.py#L187

We should default missing variables to 0, so they are filtered out at next line. We should also consider filtering off negative numbers.