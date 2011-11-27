"""Provide the 'autogenerate' feature which can produce migration operations
automatically."""

from alembic.context import _context_opts
from alembic import util
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy import types as sqltypes, schema

###################################################
# top level

def produce_migration_diffs(template_args):
    metadata = _context_opts['autogenerate_metadata']
    if metadata is None:
        raise util.CommandError(
                "Can't proceed with --autogenerate option; environment "
                "script env.py does not provide "
                "a MetaData object to the context.")
    connection = get_bind()
    diffs = []
    _produce_net_changes(connection, metadata, diffs)
    _set_upgrade(template_args, _produce_upgrade_commands(diffs))
    _set_downgrade(template_args, _produce_downgrade_commands(diffs))

def _set_upgrade(template_args, text):
    template_args[_context_opts['upgrade_token']] = text

def _set_downgrade(template_args, text):
    template_args[_context_opts['downgrade_token']] = text

###################################################
# walk structures

def _produce_net_changes(connection, metadata, diffs):
    inspector = Inspector.from_engine(connection)
    conn_table_names = set(inspector.get_table_names())
    metadata_table_names = set(metadata.tables)

    diffs.extend(
        ("upgrade_table", metadata.tables[tname])
        for tname in metadata_table_names.difference(conn_table_names)
    )
    diffs.extend(
        ("downgrade_table", tname)
        for tname in conn_table_names.difference(metadata_table_names)
    )

    existing_tables = conn_table_names.intersection(metadata_table_names)

    conn_column_info = dict(
        (tname, 
            dict(
                (rec["name"], rec)
                for rec in inspector.get_columns(tname)
            )
        )
        for tname in existing_tables
    )

    for tname in existing_tables:
        _compare_columns(tname, 
                conn_column_info[tname], 
                metadata.tables[tname],
                diffs)

    # TODO: 
    # index add/drop
    # table constraints
    # sequences

###################################################
# element comparison

def _compare_columns(tname, conn_table, metadata_table, diffs):
    metadata_cols_by_name = dict((c.name, c) for c in metadata_table.c)
    conn_col_names = set(conn_table)
    metadata_col_names = set(metadata_cols_by_name)

    diffs.extend(
        ("upgrade_column", tname, metadata_cols_by_name[cname])
        for cname in metadata_col_names.difference(conn_col_names)
    )
    diffs.extend(
        ("downgrade_column", tname, cname)
        for cname in conn_col_names.difference(metadata_col_names)
    )

    for colname in metadata_col_names.intersection(conn_col_names):
        metadata_col = metadata_table.c[colname]
        conn_col = conn_table[colname]
        _compare_type(tname, colname,
            conn_col['type'],
            metadata_col.type,
            diffs
        )
        _compare_nullable(tname, colname,
            conn_col['nullable'],
            metadata_col.nullable,
            diffs
        )

def _compare_nullable(tname, cname, conn_col_nullable, 
                            metadata_col_nullable, diffs):
    if conn_col_nullable is not metadata_col_nullable:
        diffs.extend([
            ("upgrade_nullable", tname, cname, metadata_col_nullable),
            ("downgrade_nullable", tname, cname, conn_col_nullable)
        ])

def _compare_type(tname, cname, conn_type, metadata_type, diffs):
    if conn_type._compare_type_affinity(metadata_type):
        comparator = _type_comparators.get(conn_type._type_affinity, None)

        isdiff = comparator and comparator(metadata_type, conn_type)
    else:
        isdiff = True

    if isdiff:
        diffs.extend([
            ("upgrade_type", tname, cname, metadata_type),
            ("downgrade_type", tname, cname, conn_type)
        ])

def _string_compare(t1, t2):
    return \
        t1.length is not None and \
        t1.length != t2.length

def _numeric_compare(t1, t2):
    return \
        (
            t1.precision is not None and \
            t1.precision != t2.precision
        ) or \
        (
            t1.scale is not None and \
            t1.scale != t2.scale
        )
_type_comparators = {
    sqltypes.String:_string_compare,
    sqltypes.Numeric:_numeric_compare
}

###################################################
# render python

def _produce_upgrade_commands(diffs):
    for diff in diffs:
        if diff.startswith('upgrade_'):
            cmd = _commands[diff[0]]
            cmd(*diff[1:])

def _produce_downgrade_commands(diffs):
    for diff in diffs:
        if diff.startswith('downgrade_'):
            cmd = _commands[diff[0]]
            cmd(*diff[1:])

def _upgrade_table(table):
    return \
"""create_table(%(tablename)r, 
        %(args)s
    )
""" % {
        'tablename':table.name,
        'args':',\n'.join(
            [_render_col(col) for col in table.c] +
            sorted([rcons for rcons in 
                [_render_constraint(cons) for cons in 
                    table.constraints]
                if rcons is not None
            ])
        ),
    }

def _downgrade_table(tname):
    return "drop_table(%r)" % tname

def _upgrade_column(tname, column):
    return "add_column(%r, %s)" % (
            tname, 
            _render_column(column))

def _downgrade_column(tname, cname):
    return "drop_column(%r, %r)" % (tname, cname)

def _up_or_downgrade_type(tname, cname, type_):
    return "alter_column(%r, %r, type=%r)" % (
        tname, cname, type_
    )

def _up_or_downgrade_nullable(tname, cname, nullable):
    return "alter_column(%r, %r, nullable=%r)" % (
        tname, cname, nullable
    )

_commands = {
    'upgrade_table':_upgrade_table,
    'downgrade_table':_downgrade_table,

    'upgrade_column':_upgrade_column,
    'downgrade_column':_downgrade_column,

    'upgrade_type':_up_or_downgrade_type,
    'downgrde_type':_up_or_downgrade_type,

    'upgrade_nullable':_up_or_downgrade_nullable,
    'downgrade_nullable':_up_or_downgrade_nullable,

}

def _render_col(column):
    opts = []
    if column.server_default:
        opts.append(("server_default", column.server_default))
    if column.nullable is not None:
        opts.append(("nullable", column.nullable))

    # TODO: for non-ascii colname, assign a "key"
    return "Column(%(name)r, %(type)r, %(kw)s)" % {
        'name':column.name,
        'type':column.type,
        'kw':", ".join(["%s=%s" % (kwname, val) for kwname, val in opts])
    }

def _render_constraint(constraint):
    renderer = _constraint_renderers.get(type(constraint), None)
    if renderer:
        return renderer(constraint)
    else:
        return None

def _render_primary_key(constraint):
    opts = []
    if constraint.name:
        opts.append(("name", constraint.name))
    return "PrimaryKeyConstraint(%(args)s)" % {
        "args":", ".join(
            [c.key for c in constraint.columns] +
            ["%s=%s" % (kwname, val) for kwname, val in opts]
        ),
    }

def _render_foreign_key(constraint):
    opts = []
    if constraint.name:
        opts.append(("name", constraint.name))
    # TODO: deferrable, initially, etc.
    return "ForeignKeyConstraint([%(cols)s], [%(refcols)s], %(args)s)" % {
        "cols":", ".join(f.parent.key for f in constraint.elements),
        "refcols":", ".join(repr(f._get_colspec()) for f in constraint.elements),
        "args":", ".join(
            ["%s=%s" % (kwname, val) for kwname, val in opts]
        ),
    }

def _render_check_constraint(constraint):
    opts = []
    if constraint.name:
        opts.append(("name", constraint.name))
    return "CheckConstraint('TODO')"

_constraint_renderers = {
    schema.PrimaryKeyConstraint:_render_primary_key,
    schema.ForeignKeyConstraint:_render_foreign_key,
    schema.CheckConstraint:_render_check_constraint
}
