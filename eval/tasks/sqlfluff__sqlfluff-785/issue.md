Add Support for Alternate File Types/Extensions
It would be awesome if sqlfluff could support configuring a list of file types/extensions to be considered by the `lint` and `fix` commands. Ideally, the configuration could be done in the `.sqlfluff` file.

One example would be Jinja2 files that are templated SQL statements. Using the convention of `.sql.j2	 for the file extension would allow these files to be targeted for formatting along side `.sql` files.

Thanks!