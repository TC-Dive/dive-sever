from typing import List
from unittest import mock
from django.test import TestCase

from dive.base_test import BaseTestWithDataFrameAndExcel
from apps.core.models import Snapshot, Action, Join
from utils.common import ColumnTypes
from dive.consts import JOIN_CLAUSE_OPERATIONS
from apps.core.factories import (
    DatasetFactory,
    TableFactory,
    SnapshotFactory,
    JoinFactory,
)
from apps.core.tasks import (
    extract_table_data,
    calculate_column_stats_for_action,
    create_snapshot_for_table,
    perform_join,
)
from apps.core.utils import perform_hash_join_


class TestExtractionTasks(BaseTestWithDataFrameAndExcel):
    def test_extract_table_data(self):
        table = self.dataset.table_set.first()
        assert table is not None
        assert not Snapshot.objects.filter(table=table).exists()
        extract_table_data(table.id)
        snapshot = Snapshot.objects.filter(table=table).first()
        assert snapshot is not None
        assert snapshot.version == 1

    @mock.patch("apps.core.tasks.get_composed_action_for_action_object")
    def test_calculate_column_stats_for_action_inexistent_action(
        self, composed_action_func
    ):
        inexistent_action_id = 11
        calculate_column_stats_for_action(inexistent_action_id)
        composed_action_func.assert_not_called()

    def test_calculate_column_stats_for_action(self):
        table = self.dataset.table_set.first()
        # Create snapshot first
        create_snapshot_for_table(table)

        col_key = "0"
        action = Action.objects.create(
            table=table,
            action_name="cast_column",
            parameters=[col_key, "string"],
            order=1,
        )
        assert not action.table_column_stats
        calculate_column_stats_for_action(action.id)
        action = Action.objects.get(id=action.id)
        assert action.table_column_stats
        column = next(x for x in action.table_column_stats if x["key"] == col_key)
        assert column["type"] == "string", "Column type should be changed to string"


class TestJoinTasks(TestCase):
    def setUp(self):
        self.join_type = Join.JoinType.INNER_JOIN
        (
            self.source_columns,
            self.source_stats,
            self.source_rows,
        ) = self.get_source_data()
        (
            self.target_columns,
            self.target_stats,
            self.target_rows,
        ) = self.get_target_data()
        self.clause = {
            "source_column": "id",
            "target_column": "id",
            "operation": JOIN_CLAUSE_OPERATIONS.EQUAL,
        }

    def test_join_task(self):
        dataset = DatasetFactory.create()
        [source_table, target_table] = TableFactory.create_batch(2, dataset=dataset)
        # Create snapshots

        # Create source and target table snapshots
        SnapshotFactory.create(
            version=1,
            table=source_table,
            data_rows=self.source_rows,
            data_columns=self.source_columns,
            column_stats=self.source_stats,
        )
        SnapshotFactory.create(
            version=1,
            table=target_table,
            data_rows=self.target_rows,
            data_columns=self.target_columns,
            column_stats=self.target_stats,
        )

        join_object = JoinFactory.create(
            source_table=source_table,
            target_table=target_table,
            clauses=[self.clause],
            join_type=self.join_type,
        )
        new_table = TableFactory.create(dataset=dataset, joined_from=join_object)
        assert (
            Snapshot.objects.filter(table=new_table).first() is None
        ), "There must be no snapshot"
        perform_join(new_table.id)

        # Check for snapshot and data
        joined_snapshot = Snapshot.objects.filter(table=new_table).first()
        assert (
            joined_snapshot is not None
        ), "There must be snapshot after join operation"
        self._test_expectation(
            joined_snapshot.data_columns,
            joined_snapshot.data_rows,
            joined_snapshot.column_stats,
            suffix=str(target_table.id),
        )

    def test_hash_join_inner(self):
        suffix = "_joined"
        new_cols, new_rows, new_stats = perform_hash_join_(
            self.clause,
            self.source_columns,
            self.target_columns,
            self.source_rows,
            self.target_rows,
            self.source_stats,
            self.target_stats,
            join_type=Join.JoinType.INNER_JOIN,
            conflicting_col_suffix=suffix,
        )
        self._test_expectation(new_cols, new_rows, new_stats, suffix)

    def _test_expectation(
        self, new_cols: List, new_rows: List, new_stats: List, suffix: str
    ):
        # There are 3 cols in source table and 3 in target table
        assert len(new_cols) == 6, "There must be total of 6 columns"
        new_keys = [x["key"] for x in new_cols]
        expected_keys = ["name", "id", "address", "id" + suffix, "color", "color_code"]
        assert new_keys == expected_keys, "id is redundant so named as id_joined"

        # If we look at the rows, there are 5 rows matching the join conditions
        assert len(new_rows) == 5

        assert len(new_stats) == 6, "There should be 6 entries for column stats"
        all_stats = [*self.source_stats, *self.target_stats]

        for i, stat in enumerate(new_stats):
            if i < 3 or i > 3:
                # Means source table stats and non-conflicting target keys
                # which are not changed
                assert stat == all_stats[i]
            else:
                # Means joined target id column
                # Pop the key and compare them
                # The rest fields should be the same
                assert stat.pop("key") == all_stats[i].pop("key") + suffix
                assert stat == all_stats[i]

    def get_source_data(self):
        src_col1 = {
            "key": "name",
            "label": "Name",
            "type": ColumnTypes.STRING,
        }
        src_col2 = {
            "key": "id",
            "label": "Id",
            "type": ColumnTypes.INTEGER,
        }
        src_col3 = {
            "key": "address",
            "label": "Address",
            "type": ColumnTypes.STRING,
        }
        # Set some dummy stats, these don't need to be exact stats because
        col1_stats = {
            **src_col1,
            "total_counts": 10,
            "na_counts": 3,
        }
        col2_stats = {
            **src_col2,
            "total_counts": 10,
            "na_counts": 0,
            "mean": 10.1,
        }
        col3_stats = {
            **src_col3,
            "total_counts": 10,
            "na_counts": 2,
            "max_length": 10,
        }
        columns = [src_col1, src_col2, src_col3]
        stats = [col1_stats, col2_stats, col3_stats]
        rows = [
            {"name": "Bibek", "id": 1, "address": "Chitwan"},
            {"name": "Rishi", "id": 2, "address": "Chitwan"},
            {"name": "Sameer", "id": 3, "address": "Paanchthar"},
            {"name": "Shreeyash", "id": 4, "address": "Bhaktapur"},
        ]

        return columns, stats, rows

    def get_target_data(self):
        tgt_col1 = {
            "key": "id",
            "label": "Id",
            "type": ColumnTypes.INTEGER,
        }
        tgt_col2 = {
            "key": "color",
            "label": "Color",
            "type": ColumnTypes.STRING,
        }
        tgt_col3 = {
            "key": "color_code",
            "label": "Color code",
            "type": ColumnTypes.STRING,
        }
        # Set some dummy stats, these don't need to be exact stats because
        col1_stats = {
            **tgt_col1,
            "total_counts": 10,
            "na_counts": 3,
        }
        col2_stats = {
            **tgt_col2,
            "total_counts": 10,
            "na_counts": 0,
            "mean": 10.1,
        }
        col3_stats = {
            **tgt_col3,
            "total_counts": 10,
            "na_counts": 2,
            "max_length": 10,
        }

        columns = [tgt_col1, tgt_col2, tgt_col3]
        stats = [col1_stats, col2_stats, col3_stats]
        rows = [
            {"id": 5, "color": "Blue", "color_code": "blue"},
            {"id": 1, "color": "Blue", "color_code": "blue"},
            {"id": 3, "color": "Red", "color_code": "red"},
            {"id": 3, "color": "Pink", "color_code": "pink"},
            {"id": 4, "color": "Green", "color_code": "green"},
            {"id": 4, "color": "Orange", "color_code": "orange"},
        ]
        return columns, stats, rows
