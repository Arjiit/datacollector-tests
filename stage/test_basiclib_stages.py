# Copyright 2017 StreamSets Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import string

import pytest
from streamsets.sdk.models import Configuration
from streamsets.testframework import sdc
from streamsets.testframework.utils import get_random_string

logger = logging.getLogger(__name__)

#
# Shell executor
#


@pytest.fixture(scope='module')
def sdc_shell(args):
    sdc_shell = sdc.DataCollector(version=args.pre_upgrade_sdc_version or args.sdc_version)
    #  Blocked by TEST-128
    #  sdc_shell.sdc_properties['stage.conf_com.streamsets.pipeline.stage.executor'
    #                           '.shell.impersonation_mode'] = 'current_user'

    sdc_shell.start()

    yield sdc_shell

    if sdc_shell.tear_down_on_exit:
        sdc_shell.tear_down()


@pytest.fixture(scope='module')
def pipeline_shell_generator(sdc_shell):
    builder = sdc_shell.get_pipeline_builder()

    dev_raw_data_source = builder.add_stage('Dev Raw Data Source')
    dev_raw_data_source.data_format = 'JSON'
    dev_raw_data_source.raw_data = '{}'

    shell_executor = builder.add_stage('Shell')
    shell_executor.environment_variables = Configuration(property_key='key', file='${FILE}')
    shell_executor.script = 'echo `whoami` > $file'

    dev_raw_data_source >> shell_executor

    executor_pipeline = builder.build()
    executor_pipeline.add_parameters(FILE='/')
    sdc_shell.add_pipeline(executor_pipeline)

    yield executor_pipeline


@pytest.fixture(scope='module')
def pipeline_shell_read(sdc_shell):
    builder = sdc_shell.get_pipeline_builder()

    file_source = builder.add_stage('File Tail')
    file_source.data_format = 'TEXT'
    file_source.file_to_tail = [
        dict(fileRollMode='REVERSE_COUNTER', patternForToken='.*', fileFullPath='${FILE}')
    ]

    trash1 = builder.add_stage('Trash')
    trash2 = builder.add_stage('Trash')

    file_source >> trash1
    file_source >> trash2

    read_pipeline = builder.build()
    read_pipeline.add_parameters(FILE='/')
    sdc_shell.add_pipeline(read_pipeline)

    yield read_pipeline


def test_shell_executor_impersonation(sdc_shell, pipeline_shell_generator, pipeline_shell_read):
    """Test proper impersonation on the Shell executor side.
       This is a dual pipeline test to test the executor side effect."""

    # Use this file to exchange data between the executor and our test
    runtime_parameters = {'FILE': "/tmp/{}".format(get_random_string(string.ascii_letters, 30))}

    # Run the pipeline with executor exactly once
    sdc_shell.start_pipeline(pipeline_shell_generator,
                             runtime_parameters=runtime_parameters).wait_for_pipeline_batch_count(1)
    sdc_shell.stop_pipeline(pipeline_shell_generator)

    # And retrieve its output
    snapshot = sdc_shell.capture_snapshot(pipeline=pipeline_shell_read, runtime_parameters=runtime_parameters,
                                          start_pipeline=True).snapshot
    sdc_shell.stop_pipeline(pipeline_shell_read)

    records = snapshot[pipeline_shell_read.origin_stage].output_lanes[pipeline_shell_read.origin_stage.output_lanes[0]]
    assert len(records) == 1
    # Blocked by TEST-128, should be different user
    assert records[0].value['value']['text']['value'] == 'sdc'


def test_stream_selector_processor(sdc_builder, sdc_executor):
    """Smoke test for the Stream Selector processor.

    A handful of records containing Tour de France contenders and their number of wins is passed
    to a Stream Selector with multi-winners going to one Trash stage and not multi-winners going
    to another.

                                               >> to_error
    dev_raw_data_source >> record_deduplicator >> stream_selector >> trash (multi-winners)
                                                                  >> trash (not multi-winners)
    """
    multi_winners = [dict(name='Chris Froome', wins='3'),
                     dict(name='Greg LeMond', wins='3')]
    not_multi_winners = [dict(name='Vincenzo Nibali', wins='1'),
                         dict(name='Nairo Quintana', wins='0')]

    tour_de_france_contenders = multi_winners + not_multi_winners

    pipeline_builder = sdc_builder.get_pipeline_builder()
    dev_raw_data_source = pipeline_builder.add_stage('Dev Raw Data Source')
    dev_raw_data_source.set_attributes(data_format='JSON',
                                       json_content='ARRAY_OBJECTS',
                                       raw_data=json.dumps(tour_de_france_contenders))
    record_deduplicator = pipeline_builder.add_stage('Record Deduplicator')
    to_error = pipeline_builder.add_stage('To Error')
    stream_selector = pipeline_builder.add_stage('Stream Selector')
    trash_multi_winners = pipeline_builder.add_stage('Trash')
    trash_not_multi_winners = pipeline_builder.add_stage('Trash')

    dev_raw_data_source >> record_deduplicator >> stream_selector >> trash_multi_winners
    record_deduplicator >> to_error
    stream_selector >> trash_not_multi_winners

    stream_selector.condition = [dict(outputLane=stream_selector.output_lanes[0],
                                      predicate='${record:value("/wins") > 1}'),
                                 dict(outputLane=stream_selector.output_lanes[1],
                                      predicate='default')]

    pipeline = pipeline_builder.build('test_stream_selector_processor')
    sdc_executor.add_pipeline(pipeline)

    snapshot = sdc_executor.capture_snapshot(pipeline, start_pipeline=True).snapshot
    sdc_executor.stop_pipeline(pipeline)

    multi_winners_records = snapshot[stream_selector].output_lanes[stream_selector.output_lanes[0]]
    multi_winners_from_snapshot = [{field: value['value']
                                    for field, value in record.value['value'].items()}
                                   for record in multi_winners_records]
    assert multi_winners == multi_winners_from_snapshot

    not_multi_winners_records = snapshot[stream_selector].output_lanes[stream_selector.output_lanes[1]]
    not_multi_winners_from_snapshot = [{field: value['value']
                                        for field, value in record.value['value'].items()}
                                       for record in not_multi_winners_records]
    assert not_multi_winners == not_multi_winners_from_snapshot
