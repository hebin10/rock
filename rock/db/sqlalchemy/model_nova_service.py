# Copyright 2011 OpenStack Foundation.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from sqlalchemy import Column
from sqlalchemy import Boolean
from sqlalchemy import String
from sqlalchemy.ext.declarative import declarative_base

from rock.db.sqlalchemy.model_base import ModelBase

Base = declarative_base()


class ModelNovaService(ModelBase, Base):
    __tablename__ = 'nova_service'

    service_state = Column(Boolean(), nullable=False)
    service_status = Column(Boolean(), nullable=False)
    disabled_reason = Column(String(255), nullable=True)
