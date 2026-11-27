/* 独立 Bullet 的 RB_* 兼容实现。
 *
 * 语义对齐 Blender `intern/rigidbody/rb_bullet_api.cpp`（v5.2.2 / 提交
 * d13f752e）：同一套构造参数、质量/激活状态处理、约束帧和马达映射，
 * 但只链接独立编译的 Bullet，不依赖 Blender 进程内符号。 */

#include "rb_internal.h"

#include <cstdlib>

static_assert(sizeof(btScalar) == 8,
              "独立桥需要 double 精度 Bullet（BT_USE_DOUBLE_PRECISION）");

namespace {

class RbFilterCallback : public btOverlapFilterCallback {
 public:
  bool needBroadphaseCollision(btBroadphaseProxy *proxy0,
                               btBroadphaseProxy *proxy1) const override
  {
    auto *rb0 = static_cast<rbRigidBody *>(
        static_cast<btRigidBody *>(proxy0->m_clientObject)->getUserPointer());
    auto *rb1 = static_cast<rbRigidBody *>(
        static_cast<btRigidBody *>(proxy1->m_clientObject)->getUserPointer());

    bool collides = (proxy0->m_collisionFilterGroup & proxy1->m_collisionFilterMask) != 0;
    collides = collides && ((proxy1->m_collisionFilterGroup & proxy0->m_collisionFilterMask) != 0);
    collides = collides && ((rb0->col_groups & rb1->col_groups) != 0);
    return collides;
  }
};

rbCollisionShape *wrap_shape(btCollisionShape *cshape)
{
  auto *shape = new rbCollisionShape;
  shape->cshape = cshape;
  shape->mesh = nullptr;
  shape->compound_child_shapes = nullptr;
  shape->compound_children = 0;
  return shape;
}

void delete_mesh_data(rbMeshData *mesh)
{
  delete mesh->index_array;
  delete[] mesh->vertices;
  delete[] mesh->triangles;
  delete mesh;
}

}  // namespace

/* ------------------------------------------------------------------ */
/* 动力学世界                                                          */
/* ------------------------------------------------------------------ */

rbDynamicsWorld *RB_dworld_new(const float gravity[3])
{
  auto *world = new rbDynamicsWorld;

  world->collision_configuration = new btDefaultCollisionConfiguration();
  world->dispatcher = new btCollisionDispatcher(world->collision_configuration);
  btGImpactCollisionAlgorithm::registerAlgorithm(
      static_cast<btCollisionDispatcher *>(world->dispatcher));

  world->broadphase = new btDbvtBroadphase();
  world->filter_callback = new RbFilterCallback();
  world->broadphase->getOverlappingPairCache()->setOverlapFilterCallback(world->filter_callback);

  world->constraint_solver = new btSequentialImpulseConstraintSolver();
  world->dynamics_world = new btDiscreteDynamicsWorld(world->dispatcher,
                                                      world->broadphase,
                                                      world->constraint_solver,
                                                      world->collision_configuration);
  RB_dworld_set_gravity(world, gravity);
  return world;
}

void RB_dworld_delete(rbDynamicsWorld *world)
{
  if (!world) {
    return;
  }
  /* 顺序与 Bullet 内部所有权一致：先世界，再求解器、宽相、分发器、配置、过滤器。 */
  delete world->dynamics_world;
  delete world->constraint_solver;
  delete world->broadphase;
  delete world->dispatcher;
  delete world->collision_configuration;
  delete world->filter_callback;
  delete world;
}

void RB_dworld_get_gravity(rbDynamicsWorld *world, float g_out[3])
{
  from_bt_vec(g_out, world->dynamics_world->getGravity());
}

void RB_dworld_set_gravity(rbDynamicsWorld *world, const float g_in[3])
{
  world->dynamics_world->setGravity(to_bt_vec(g_in));
}

void RB_dworld_set_solver_iterations(rbDynamicsWorld *world, int iterations)
{
  world->dynamics_world->getSolverInfo().m_numIterations = iterations;
}

void RB_dworld_set_split_impulse(rbDynamicsWorld *world, int enabled)
{
  world->dynamics_world->getSolverInfo().m_splitImpulse = enabled != 0;
}

void RB_dworld_step_simulation(rbDynamicsWorld *world,
                               float time_step,
                               int max_sub_steps,
                               float fixed_time_step)
{
  world->dynamics_world->stepSimulation(time_step, max_sub_steps, fixed_time_step);
}

void RB_dworld_add_body(rbDynamicsWorld *world, rbRigidBody *body, int col_groups)
{
  body->col_groups = col_groups;
  world->dynamics_world->addRigidBody(body->body);
}

void RB_dworld_remove_body(rbDynamicsWorld *world, rbRigidBody *body)
{
  world->dynamics_world->removeRigidBody(body->body);
}

/* ------------------------------------------------------------------ */
/* 刚体                                                                */
/* ------------------------------------------------------------------ */

rbRigidBody *RB_body_new(rbCollisionShape *shape, const float loc[3], const float rot[4])
{
  auto *object = new rbRigidBody;
  const btTransform transform = to_bt_transform(loc, rot);

  auto *motion_state = new btDefaultMotionState(transform);
  btRigidBody::btRigidBodyConstructionInfo info(1.0f, motion_state, shape->cshape);
  object->body = new btRigidBody(info);
  object->body->setUserPointer(object);
  object->col_groups = 1;
  return object;
}

void RB_body_delete(rbRigidBody *object)
{
  if (!object) {
    return;
  }
  btRigidBody *body = object->body;
  delete body->getMotionState();
  for (int i = body->getNumConstraintRefs() - 1; i >= 0; --i) {
    body->removeConstraintRef(body->getConstraintRef(i));
  }
  delete body;
  delete object;
}

float RB_body_get_mass(rbRigidBody *object)
{
  const btScalar inverse_mass = object->body->getInvMass();
  return inverse_mass != 0.0f ? static_cast<float>(1.0f / inverse_mass) : 0.0f;
}

void RB_body_set_mass(rbRigidBody *object, float mass)
{
  btRigidBody *body = object->body;
  btVector3 local_inertia(0, 0, 0);
  if (mass != 0.0f) {
    body->getCollisionShape()->calculateLocalInertia(mass, local_inertia);
  }
  body->setMassProps(mass, local_inertia);
  body->updateInertiaTensor();
}

float RB_body_get_friction(rbRigidBody *object)
{
  return object->body->getFriction();
}

void RB_body_set_friction(rbRigidBody *object, float friction)
{
  object->body->setFriction(friction);
}

float RB_body_get_restitution(rbRigidBody *object)
{
  return object->body->getRestitution();
}

void RB_body_set_restitution(rbRigidBody *object, float restitution)
{
  object->body->setRestitution(restitution);
}

void RB_body_set_damping(rbRigidBody *object, float linear, float angular)
{
  object->body->setDamping(linear, angular);
}

void RB_body_set_sleep_thresh(rbRigidBody *object, float linear, float angular)
{
  object->body->setSleepingThresholds(linear, angular);
}

void RB_body_set_activation_state(rbRigidBody *object, int use_deactivation)
{
  btRigidBody *body = object->body;
  if (use_deactivation) {
    body->forceActivationState(ACTIVE_TAG);
  }
  else {
    body->setActivationState(DISABLE_DEACTIVATION);
  }
}

void RB_body_activate(rbRigidBody *object)
{
  object->body->setActivationState(ACTIVE_TAG);
}

void RB_body_deactivate(rbRigidBody *object)
{
  object->body->setActivationState(ISLAND_SLEEPING);
}

void RB_body_get_linear_velocity(rbRigidBody *object, float v_out[3])
{
  from_bt_vec(v_out, object->body->getLinearVelocity());
}

void RB_body_set_linear_velocity(rbRigidBody *object, const float v_in[3])
{
  object->body->setLinearVelocity(to_bt_vec(v_in));
}

void RB_body_get_angular_velocity(rbRigidBody *object, float v_out[3])
{
  from_bt_vec(v_out, object->body->getAngularVelocity());
}

void RB_body_set_angular_velocity(rbRigidBody *object, const float v_in[3])
{
  object->body->setAngularVelocity(to_bt_vec(v_in));
}

void RB_body_set_linear_factor(rbRigidBody *object, float x, float y, float z)
{
  object->body->setLinearFactor(btVector3(x, y, z));
}

void RB_body_set_angular_factor(rbRigidBody *object, float x, float y, float z)
{
  object->body->setAngularFactor(btVector3(x, y, z));
}

void RB_body_set_loc_rot(rbRigidBody *object, const float loc[3], const float rot[4])
{
  btMotionState *motion_state = object->body->getMotionState();
  if (motion_state) {
    motion_state->setWorldTransform(to_bt_transform(loc, rot));
  }
  else {
    object->body->setWorldTransform(to_bt_transform(loc, rot));
  }
}

void RB_body_get_position(rbRigidBody *object, float v_out[3])
{
  from_bt_vec(v_out, object->body->getWorldTransform().getOrigin());
}

void RB_body_get_orientation(rbRigidBody *object, float v_out[4])
{
  from_bt_quat(v_out, object->body->getWorldTransform().getRotation());
}

void RB_body_get_transform_matrix(rbRigidBody *object, float m_out[4][4])
{
  btTransform transform;
  btMotionState *motion_state = object->body->getMotionState();
  if (motion_state) {
    motion_state->getWorldTransform(transform);
  }
  else {
    transform = object->body->getWorldTransform();
  }
  btScalar matrix[16];
  transform.getOpenGLMatrix(matrix);
  /* 对外始终是 float[4][4]；不能像 float 时代那样把 btScalar* 直接当输出。 */
  for (int i = 0; i < 16; ++i) {
    m_out[i / 4][i % 4] = static_cast<float>(matrix[i]);
  }
}

void RB_body_apply_central_force(rbRigidBody *object, const float v_in[3])
{
  object->body->applyCentralForce(to_bt_vec(v_in));
}

/* ------------------------------------------------------------------ */
/* 碰撞形状                                                            */
/* ------------------------------------------------------------------ */

rbCollisionShape *RB_shape_new_box(float x, float y, float z)
{
  return wrap_shape(new btBoxShape(btVector3(x, y, z)));
}

rbCollisionShape *RB_shape_new_compound(void)
{
  return wrap_shape(new btCompoundShape());
}

void RB_compound_add_child_shape(rbCollisionShape *parent,
                                 rbCollisionShape *child,
                                 const float loc[3],
                                 const float rot[4])
{
  auto *compound = static_cast<btCompoundShape *>(parent->cshape);
  compound->addChildShape(to_bt_transform(loc, rot), child->cshape);

  parent->compound_child_shapes = static_cast<rbCollisionShape **>(std::realloc(
      parent->compound_child_shapes,
      sizeof(rbCollisionShape *) * static_cast<size_t>(parent->compound_children + 1)));
  parent->compound_child_shapes[parent->compound_children] = child;
  ++parent->compound_children;
}

rbCollisionShape *RB_shape_new_convex_hull(
    const float *vertices, int stride, int count, float margin, bool *can_embed)
{
  btConvexHullComputer hull;
  if (hull.compute(vertices, stride, count, margin, 0.0f) < 0.0f) {
    hull.compute(vertices, stride, count, 0.0f, 0.0f);
    if (can_embed) {
      *can_embed = false;
    }
  }

  btConvexHullShape *hull_shape;
  if (hull.vertices.size() > 0) {
    hull_shape = new btConvexHullShape(
        reinterpret_cast<const btScalar *>(&hull.vertices[0]), hull.vertices.size());
    /* Bullet 3.25 的 GJK/EPA 在极小 margin（消费者用 5e-5）下会抖动/穿透；
     * 预生成 polyhedron 后窄相直接使用凸包面，几何与零 margin 时一致。 */
    hull_shape->initializePolyhedralFeatures();
  }
  else {
    hull_shape = new btConvexHullShape();
  }
  return wrap_shape(hull_shape);
}

rbMeshData *RB_trimesh_data_new(int num_tris, int num_verts)
{
  auto *mesh = new rbMeshData;
  mesh->vertices = new rbVert[num_verts > 0 ? num_verts : 1];
  mesh->triangles = new rbTri[num_tris > 0 ? num_tris : 1];
  mesh->num_vertices = num_verts;
  mesh->num_triangles = num_tris;
  mesh->index_array = nullptr;
  return mesh;
}

void RB_trimesh_add_vertices(rbMeshData *mesh, float *vertices, int num_verts, int vertex_stride)
{
  for (int i = 0; i < num_verts && i < mesh->num_vertices; ++i) {
    const auto *source = reinterpret_cast<const float *>(
        reinterpret_cast<const char *>(vertices) + static_cast<size_t>(i) * vertex_stride);
    mesh->vertices[i].x = static_cast<btScalar>(source[0]);
    mesh->vertices[i].y = static_cast<btScalar>(source[1]);
    mesh->vertices[i].z = static_cast<btScalar>(source[2]);
  }
}

void RB_trimesh_add_triangle_indices(
    rbMeshData *mesh, int num, int index0, int index1, int index2)
{
  if (num < 0 || num >= mesh->num_triangles) {
    return;
  }
  mesh->triangles[num].v0 = index0;
  mesh->triangles[num].v1 = index1;
  mesh->triangles[num].v2 = index2;
}

void RB_trimesh_finish(rbMeshData *mesh)
{
  mesh->index_array = new btTriangleIndexVertexArray(mesh->num_triangles,
                                                     reinterpret_cast<int *>(mesh->triangles),
                                                     sizeof(rbTri),
                                                     mesh->num_vertices,
                                                     reinterpret_cast<btScalar *>(mesh->vertices),
                                                     sizeof(rbVert));
}

rbCollisionShape *RB_shape_new_trimesh(rbMeshData *mesh)
{
  auto *unscaled = new btBvhTriangleMeshShape(mesh->index_array, true, true);
  rbCollisionShape *shape = wrap_shape(
      new btScaledBvhTriangleMeshShape(unscaled, btVector3(1.0f, 1.0f, 1.0f)));
  shape->mesh = mesh;
  return shape;
}

rbCollisionShape *RB_shape_new_gimpact_mesh(rbMeshData *mesh)
{
  auto *gimpact = new btGImpactMeshShape(mesh->index_array);
  gimpact->updateBound();
  rbCollisionShape *shape = wrap_shape(gimpact);
  shape->mesh = mesh;
  return shape;
}

void RB_shape_delete(rbCollisionShape *shape)
{
  if (!shape) {
    return;
  }
  if (shape->cshape->getShapeType() == SCALED_TRIANGLE_MESH_SHAPE_PROXYTYPE) {
    delete static_cast<btScaledBvhTriangleMeshShape *>(shape->cshape)->getChildShape();
  }
  if (shape->mesh) {
    delete_mesh_data(shape->mesh);
  }
  delete shape->cshape;

  for (int i = 0; i < shape->compound_children; ++i) {
    RB_shape_delete(shape->compound_child_shapes[i]);
  }
  if (shape->compound_child_shapes) {
    std::free(shape->compound_child_shapes);
  }
  delete shape;
}

float RB_shape_get_margin(rbCollisionShape *shape)
{
  return shape->cshape->getMargin();
}

void RB_shape_set_margin(rbCollisionShape *shape, float margin)
{
  shape->cshape->setMargin(margin);
  if (shape->cshape->getShapeType() == GIMPACT_SHAPE_PROXYTYPE) {
    static_cast<btGImpactMeshShape *>(shape->cshape)->updateBound();
  }
}

/* ------------------------------------------------------------------ */
/* 约束                                                                */
/* ------------------------------------------------------------------ */

void RB_dworld_add_constraint(rbDynamicsWorld *world,
                              rbConstraint *constraint,
                              int disable_collisions)
{
  world->dynamics_world->addConstraint(reinterpret_cast<btTypedConstraint *>(constraint),
                                       disable_collisions != 0);
}

void RB_dworld_remove_constraint(rbDynamicsWorld *world, rbConstraint *constraint)
{
  world->dynamics_world->removeConstraint(reinterpret_cast<btTypedConstraint *>(constraint));
}

rbConstraint *RB_constraint_new_point(const float pivot[3],
                                      rbRigidBody *body1,
                                      rbRigidBody *body2)
{
  const btVector3 world_pivot = to_bt_vec(pivot);
  const btVector3 pivot1 = body1->body->getWorldTransform().inverse() * world_pivot;
  const btVector3 pivot2 = body2->body->getWorldTransform().inverse() * world_pivot;
  return reinterpret_cast<rbConstraint *>(
      new btPoint2PointConstraint(*body1->body, *body2->body, pivot1, pivot2));
}

rbConstraint *RB_constraint_new_fixed(float pivot[3],
                                      float orn[4],
                                      rbRigidBody *body1,
                                      rbRigidBody *body2)
{
  btTransform frame1, frame2;
  make_constraint_frames(frame1, frame2, body1->body, body2->body, pivot, orn);
  return reinterpret_cast<rbConstraint *>(
      new btFixedConstraint(*body1->body, *body2->body, frame1, frame2));
}

rbConstraint *RB_constraint_new_hinge(float pivot[3],
                                      float orn[4],
                                      rbRigidBody *body1,
                                      rbRigidBody *body2)
{
  btTransform frame1, frame2;
  make_constraint_frames(frame1, frame2, body1->body, body2->body, pivot, orn);
  return reinterpret_cast<rbConstraint *>(
      new btHingeConstraint(*body1->body, *body2->body, frame1, frame2));
}

rbConstraint *RB_constraint_new_slider(float pivot[3],
                                       float orn[4],
                                       rbRigidBody *body1,
                                       rbRigidBody *body2)
{
  btTransform frame1, frame2;
  make_constraint_frames(frame1, frame2, body1->body, body2->body, pivot, orn);
  return reinterpret_cast<rbConstraint *>(new btSliderConstraint(
      *body1->body, *body2->body, frame1, frame2, true));
}

rbConstraint *RB_constraint_new_piston(float pivot[3],
                                       float orn[4],
                                       rbRigidBody *body1,
                                       rbRigidBody *body2)
{
  btTransform frame1, frame2;
  make_constraint_frames(frame1, frame2, body1->body, body2->body, pivot, orn);
  auto *slider = new btSliderConstraint(*body1->body, *body2->body, frame1, frame2, true);
  slider->setUpperAngLimit(-1.0f);  /* 解锁旋转轴 */
  return reinterpret_cast<rbConstraint *>(slider);
}

rbConstraint *RB_constraint_new_6dof(float pivot[3],
                                     float orn[4],
                                     rbRigidBody *body1,
                                     rbRigidBody *body2)
{
  btTransform frame1, frame2;
  make_constraint_frames(frame1, frame2, body1->body, body2->body, pivot, orn);
  return reinterpret_cast<rbConstraint *>(
      new btGeneric6DofConstraint(*body1->body, *body2->body, frame1, frame2, true));
}

rbConstraint *RB_constraint_new_6dof_spring(float pivot[3],
                                            float orn[4],
                                            rbRigidBody *body1,
                                            rbRigidBody *body2)
{
  btTransform frame1, frame2;
  make_constraint_frames(frame1, frame2, body1->body, body2->body, pivot, orn);
  return reinterpret_cast<rbConstraint *>(
      new btGeneric6DofSpringConstraint(*body1->body, *body2->body, frame1, frame2, true));
}

rbConstraint *RB_constraint_new_6dof_spring2(float pivot[3],
                                             float orn[4],
                                             rbRigidBody *body1,
                                             rbRigidBody *body2)
{
  btTransform frame1, frame2;
  make_constraint_frames(frame1, frame2, body1->body, body2->body, pivot, orn);
  return reinterpret_cast<rbConstraint *>(
      new btGeneric6DofSpring2Constraint(*body1->body, *body2->body, frame1, frame2));
}

rbConstraint *RB_constraint_new_motor(float pivot[3],
                                      float orn[4],
                                      rbRigidBody *body1,
                                      rbRigidBody *body2)
{
  btTransform frame1, frame2;
  make_constraint_frames(frame1, frame2, body1->body, body2->body, pivot, orn);
  auto *constraint = new btGeneric6DofConstraint(
      *body1->body, *body2->body, frame1, frame2, true);

  /* Blender 的马达约束：六个轴先解锁，仅靠 enable_motor 打开线性/角度马达。 */
  for (int axis = 0; axis < 6; ++axis) {
    constraint->setLimit(axis, 0.0f, -1.0f);
  }
  constraint->getTranslationalLimitMotor()->m_upperLimit.setValue(-1.0f, -1.0f, -1.0f);
  return reinterpret_cast<rbConstraint *>(constraint);
}

void RB_constraint_delete(rbConstraint *constraint)
{
  if (!constraint) {
    return;
  }
  auto *typed = reinterpret_cast<btTypedConstraint *>(constraint);
  typed->getRigidBodyA().removeConstraintRef(typed);
  typed->getRigidBodyB().removeConstraintRef(typed);
  delete typed;
}

void RB_constraint_set_enabled(rbConstraint *constraint, int enabled)
{
  reinterpret_cast<btTypedConstraint *>(constraint)->setEnabled(enabled != 0);
}

void RB_constraint_set_limits_hinge(rbConstraint *constraint, float lower, float upper)
{
  reinterpret_cast<btHingeConstraint *>(constraint)->setLimit(lower, upper, 0.9f, 0.3f, 1.0f);
}

void RB_constraint_set_limits_slider(rbConstraint *constraint, float lower, float upper)
{
  auto *slider = reinterpret_cast<btSliderConstraint *>(constraint);
  slider->setLowerLinLimit(lower);
  slider->setUpperLinLimit(upper);
}

void RB_constraint_set_limits_piston(rbConstraint *constraint,
                                     float lin_lower,
                                     float lin_upper,
                                     float ang_lower,
                                     float ang_upper)
{
  auto *slider = reinterpret_cast<btSliderConstraint *>(constraint);
  slider->setLowerLinLimit(lin_lower);
  slider->setUpperLinLimit(lin_upper);
  slider->setLowerAngLimit(ang_lower);
  slider->setUpperAngLimit(ang_upper);
}

void RB_constraint_set_limits_6dof(rbConstraint *constraint,
                                   int axis,
                                   float lower,
                                   float upper)
{
  reinterpret_cast<btGeneric6DofConstraint *>(constraint)->setLimit(axis, lower, upper);
}

void RB_constraint_set_enable_motor(rbConstraint *constraint, int enable_lin, int enable_ang)
{
  auto *generic = reinterpret_cast<btGeneric6DofConstraint *>(constraint);
  generic->getTranslationalLimitMotor()->m_enableMotor[0] = enable_lin != 0;
  generic->getRotationalLimitMotor(0)->m_enableMotor = enable_ang != 0;
}

void RB_constraint_set_max_impulse_motor(rbConstraint *constraint,
                                         float max_impulse_lin,
                                         float max_impulse_ang)
{
  auto *generic = reinterpret_cast<btGeneric6DofConstraint *>(constraint);
  generic->getTranslationalLimitMotor()->m_maxMotorForce.setX(max_impulse_lin);
  generic->getRotationalLimitMotor(0)->m_maxMotorForce = max_impulse_ang;
}

void RB_constraint_set_target_velocity_motor(rbConstraint *constraint,
                                             float velocity_lin,
                                             float velocity_ang)
{
  auto *generic = reinterpret_cast<btGeneric6DofConstraint *>(constraint);
  generic->getTranslationalLimitMotor()->m_targetVelocity.setX(velocity_lin);
  generic->getRotationalLimitMotor(0)->m_targetVelocity = velocity_ang;
}

void RB_constraint_set_solver_iterations(rbConstraint *constraint, int iterations)
{
  reinterpret_cast<btTypedConstraint *>(constraint)->setOverrideNumSolverIterations(iterations);
}

void RB_constraint_set_breaking_threshold(rbConstraint *constraint, float threshold)
{
  reinterpret_cast<btTypedConstraint *>(constraint)->setBreakingImpulseThreshold(threshold);
}
