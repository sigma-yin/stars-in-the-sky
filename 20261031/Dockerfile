FROM ros:humble-ros-base

SHELL ["/bin/bash", "-c"]

WORKDIR /work/rmtc_ws

COPY src ./src
RUN source /opt/ros/humble/setup.bash && colcon build --symlink-install

COPY run_contestant.sh decision_config.json fastdds_udp_only.xml ./
RUN chmod +x run_contestant.sh

ENV ROS_DOMAIN_ID=27 \
    ROS_LOCALHOST_ONLY=0 \
    RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
    FASTRTPS_DEFAULT_PROFILES_FILE=/work/rmtc_ws/fastdds_udp_only.xml \
    RMTC_SEAT=A

ENTRYPOINT ["./run_contestant.sh"]
